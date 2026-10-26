"""The user-rebindable keymap authority — ONE source for palette, help, and TUI.

WHY THIS MODULE EXISTS
----------------------
The keymap used to live only as raw Textual ``BINDINGS`` rows plus a closed
``SHORTCUT_KEYS`` frozenset in ``cli/commands.py``. The closed set existed for
one reason: drift prevention. A typo in a key name would construct happily and
render as a key that does nothing, and the palette, ``/help``, and the TUI
could disagree about what a key does. The two-direction drift gate in
``tests/test_cli_command_system.py`` keeps those three honest — but nothing
could be rebound.

This module is the bottom-layer authority that lets a user rebind keys
WITHOUT breaking the drift guarantee. It replaces the closed *set* of eight
keys with a closed *grammar* (modifier chords over a closed base-key
vocabulary), declares the reserved keys that may never move (and why each is
reserved), validates a user-supplied ``keybindings.json`` fail-closed, and
projects the resolved keymap into the exact shapes the palette, ``/help`` and
the TUI consume. All three surfaces read the SAME resolved map, so a user
binding cannot drift between them: one source, three projections.

SCOPE AND ASSUMPTIONS
---------------------
- This module imports nothing from any package that imports it. It is the
  bottom layer; ``cli/`` consumes it. The default table below mirrors the
  current ``cli/commands.py`` tables and is pinned byte-for-byte against them
  by ``tests/test_shared_keymap.py`` so the two cannot silently disagree
  before the CLI mount lands.
- Validation is FAIL-CLOSED: an unknown action, an unknown or untypable key,
  a rebound reserved key, a cross-action key collision, or an unparseable
  file rejects the WHOLE user document and yields the default keymap plus a
  clear error list. There is never a half-applied keymap.
- A corrupt keymap must never brick the TUI: :func:`load_keymap_state` never
  raises.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ACTIONS",
    "ACTION_IDS",
    "BASE_KEYS",
    "DEFAULT_BINDINGS",
    "KEYBINDINGS_FILENAME",
    "Keymap",
    "KeymapError",
    "KeymapState",
    "MODIFIERS",
    "RESERVED_ACTIONS",
    "SCHEMA_VERSION",
    "UNTYPABLE_CHORDS",
    "default_keybindings_path",
    "keymap_invariant_findings",
    "load_keymap_state",
    "load_user_keymap",
    "normalize_key",
    "resolve_keymap",
    "validate_user_document",
]

SCHEMA_VERSION = 1
KEYBINDINGS_FILENAME = "keybindings.json"

# ---------------------------------------------------------------------------
# The key grammar
# ---------------------------------------------------------------------------
#
# The closed set of eight keys could only ever shrink. The grammar is the
# drift-safe replacement: a key is valid iff it is one or two DISTINCT
# modifiers from a closed set plus a base key from a closed set. A typo
# (``ctrl+banana``, ``crtl+g``) is still rejected at load, which is the
# property the closed set existed for.

MODIFIERS: Tuple[str, ...] = ("ctrl", "shift", "alt")

#: Single-character bases plus the named keys Textual delivers. A base NOT in
#: this set is a typo, and a typo is a key that does nothing — rejected.
BASE_KEYS: frozenset = frozenset(
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(d) for d in range(10)]
    + [f"f{n}" for n in range(1, 13)]
    + [
        "space",
        "tab",
        "enter",
        "escape",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
    ]
)

_KEY_RX = re.compile(r"^([a-z]+)(\+([a-z]+))?\+([a-z0-9]+)$")

#: Chords the terminal itself swallows, so binding them is a dead key with a
#: confident label. Each carries the reason, because "refused" without "why"
#: is how a user files the keymap as broken.
UNTYPABLE_CHORDS: Mapping[str, str] = {
    "ctrl+i": "the terminal delivers ctrl+i as Tab; the binding would never fire",
    "ctrl+m": "the terminal delivers ctrl+m as Enter; the binding would never fire",
    "ctrl+j": "the terminal delivers ctrl+j as a line feed; the binding would never fire",
    "ctrl+h": "the terminal delivers ctrl+h as Backspace; the binding would never fire",
    "ctrl+s": "ctrl+s is XOFF flow control; it freezes the output stream",
    "ctrl+v": "ctrl+v is the terminal's paste chord; binding it shadows paste",
}


def normalize_key(key: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(canonical_key, error)``. Exactly one of the two is non-None.

    Assumes ``key`` is a user-supplied string in the closed grammar. Canonical
    form is lowercase with modifiers sorted to ``ctrl, shift, alt`` order, so
    ``shift+ctrl+g`` and ``ctrl+shift+g`` are the same key and cannot be
    double-bound.
    """
    if not isinstance(key, str):
        return None, f"a key must be a string, got {type(key).__name__}"
    text = key.strip().casefold()
    match = _KEY_RX.match(text)
    if not match:
        return None, (
            f"unknown key {key!r}: keys look like 'ctrl+g', 'shift+pageup' or "
            "'ctrl+shift+f5' — one or two modifiers plus a base key"
        )
    first, _, second, base = match.groups()
    modifiers = [m for m in (first, second) if m]
    for modifier in modifiers:
        if modifier not in MODIFIERS:
            return None, (
                f"unknown modifier {modifier!r} in {key!r} "
                f"(known: {', '.join(MODIFIERS)})"
            )
    if len(set(modifiers)) != len(modifiers):
        return None, f"repeated modifier in {key!r}"
    if base not in BASE_KEYS:
        return None, f"unknown base key {base!r} in {key!r}"
    ordered = [m for m in MODIFIERS if m in modifiers]
    canonical = "+".join([*ordered, base])
    if canonical in UNTYPABLE_CHORDS:
        return None, f"{canonical} cannot be bound: {UNTYPABLE_CHORDS[canonical]}"
    return canonical, None


# ---------------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One bindable (or deliberately unbindable) thing a key can do.

    :param action_id: the stable name a ``keybindings.json`` binding names.
    :param group: ``command`` | ``surface`` | ``navigation`` — the three
        groups the drift gate already knows.
    :param name: the command name (``/steer``) or surface name (``palette``).
    :param summary: the command summary, for the commands projection.
    :param default_keys: ``((key, purpose), ...)`` in display order. The first
        key's purpose is also the action's own purpose, used when a user
        binding moves the action to a new key.
    :param reserved: when True, the action's keys may not move and no other
        action may take them.
    :param reserved_reason: WHY it is reserved. Required when ``reserved`` —
        a reserved key without a reason is a rule nobody can defend.
    """

    action_id: str
    group: str
    name: str
    summary: str
    default_keys: Tuple[Tuple[str, str], ...]
    reserved: bool = False
    reserved_reason: str = ""

    def __post_init__(self) -> None:
        if self.group not in ("command", "surface", "navigation"):
            raise ValueError(f"unknown keymap group: {self.group}")
        if self.reserved and not self.reserved_reason:
            raise ValueError(f"reserved action {self.action_id} gives no reason")
        if not self.default_keys and self.reserved:
            raise ValueError(f"reserved action {self.action_id} declares no key")


ACTIONS: Tuple[Action, ...] = (
    Action(
        "steer",
        "command",
        "/steer",
        "steer the running task",
        (
            ("ctrl+g", "steer at the next safe boundary"),
            ("ctrl+b", "queue steering without interrupting the run"),
        ),
    ),
    Action(
        "cancel",
        "command",
        "/cancel",
        "cancel the current task safely",
        (
            ("ctrl+x", "cancel the run (checkpoints kept)"),
            ("ctrl+c", "cancel the run (checkpoints kept), or quit when idle"),
        ),
        reserved=True,
        reserved_reason=(
            "the interrupt pair. ctrl+c is the OS's own SIGINT convention and "
            "the key a user reaches for when everything else is misbehaving; "
            "ctrl+x is the cancel every footer and /help row teaches. Moving "
            "either is how a runaway run keeps running and work is lost."
        ),
    ),
    Action(
        "quit",
        "command",
        "/quit",
        "quit after safely handling any active run",
        (("ctrl+q", "quit the session"),),
        reserved=True,
        reserved_reason=(
            "the exit. A user who cannot quit has lost control of the process; "
            "the quit key must never move."
        ),
    ),
    Action(
        "palette",
        "surface",
        "palette",
        "",
        (("ctrl+p", "open the command palette"),),
    ),
    Action(
        "history",
        "surface",
        "history",
        "",
        (("ctrl+r", "search the input history"),),
    ),
    Action(
        "copy",
        "surface",
        "copy",
        "",
        (("ctrl+y", "copy the selected text"),),
    ),
    Action(
        "sidebar",
        "surface",
        "sidebar",
        "",
        (("ctrl+5", "show, hide, or auto-size the sidebar"),),
    ),
    Action(
        "density",
        "surface",
        "density",
        "",
        (("ctrl+o", "switch between comfortable and compact density"),),
    ),
    Action(
        "complete_mention",
        "navigation",
        "complete_mention",
        "",
        (("ctrl+space", "complete an @path mention under the cursor"),),
    ),
    Action(
        "scroll_back",
        "navigation",
        "scroll_back",
        "",
        (("shift+up", "scroll the transcript back"),),
    ),
    Action(
        "scroll_forward",
        "navigation",
        "scroll_forward",
        "",
        (("shift+down", "scroll the transcript forward"),),
    ),
    Action(
        "scroll_page_back",
        "navigation",
        "scroll_page_back",
        "",
        (("shift+pageup", "scroll the transcript back a page"),),
    ),
    Action(
        "scroll_page_forward",
        "navigation",
        "scroll_page_forward",
        "",
        (("shift+pagedown", "scroll the transcript forward a page"),),
    ),
    Action(
        "scroll_newest",
        "navigation",
        "scroll_newest",
        "",
        (("shift+end", "jump back to the newest output"),),
    ),
)

ACTION_IDS: frozenset = frozenset(action.action_id for action in ACTIONS)

#: The actions whose keys may never move, ``action_id -> reason``.
RESERVED_ACTIONS: Mapping[str, str] = {
    action.action_id: action.reserved_reason for action in ACTIONS if action.reserved
}

#: ``action_id -> (keys, ...)`` — the shipped defaults.
DEFAULT_BINDINGS: Mapping[str, Tuple[str, ...]] = {
    action.action_id: tuple(key for key, _ in action.default_keys) for action in ACTIONS
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeymapError:
    """One validation finding. A list of these is the fail-closed refusal."""

    reason: str
    detail: str = ""

    def render(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def _binding_keys(
    action: Action, value: Any, errors: List[KeymapError]
) -> Optional[Tuple[str, ...]]:
    """Validate one binding value. ``None`` means the document is rejected."""
    if value is None or value == []:
        # Unbinding is allowed for non-reserved actions only: the palette and
        # /help already render a command with no key as an empty shortcut
        # column, and a reserved action with no key is the exact work-loss
        # state the reserved set exists to prevent.
        if action.reserved:
            errors.append(
                KeymapError(
                    "reserved key unbound",
                    f"{action.action_id} is reserved and cannot be unbound: "
                    f"{action.reserved_reason}",
                )
            )
            return None
        return ()
    raw: Sequence[Any]
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        errors.append(
            KeymapError(
                "invalid binding",
                f"{action.action_id}: a binding is a key string, a list of key "
                f"strings, or null; got {type(value).__name__}",
            )
        )
        return None
    keys: List[str] = []
    for item in raw:
        canonical, error = normalize_key(item)
        if error:
            errors.append(KeymapError("unknown key", f"{action.action_id}: {error}"))
            return None
        assert canonical is not None
        if canonical in keys:
            errors.append(
                KeymapError(
                    "duplicate key",
                    f"{action.action_id} binds {canonical} twice",
                )
            )
            return None
        keys.append(canonical)
    if action.reserved and tuple(keys) != DEFAULT_BINDINGS[action.action_id]:
        errors.append(
            KeymapError(
                "reserved key rebound",
                f"{action.action_id} is reserved: {action.reserved_reason}",
            )
        )
        return None
    return tuple(keys)


def validate_user_document(document: Any) -> List[KeymapError]:
    """Validate a parsed ``keybindings.json`` document. Total: never raises.

    ANY finding rejects the WHOLE document — a half-applied keymap (some
    bindings moved, some defaults) is the exact drift state this module
    exists to prevent, so the caller must treat one error as "defaults plus
    these errors".
    """
    errors: List[KeymapError] = []
    if not isinstance(document, Mapping):
        return [KeymapError("not a keymap", "the keymap file is not a JSON object")]
    unknown_top = set(document) - {"schema_version", "bindings"}
    if unknown_top:
        errors.append(
            KeymapError(
                "unknown keymap field",
                "unknown top-level field(s): " + ", ".join(sorted(unknown_top)),
            )
        )
    version = document.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        errors.append(
            KeymapError(
                "unsupported schema version",
                f"expected {SCHEMA_VERSION}, got {version!r}",
            )
        )
    bindings = document.get("bindings")
    if not isinstance(bindings, Mapping):
        errors.append(KeymapError("not a keymap", "'bindings' is not an object"))
        return errors
    by_id = {action.action_id: action for action in ACTIONS}
    seen_keys: Dict[str, str] = {}
    for action_id, value in bindings.items():
        action = by_id.get(str(action_id))
        if action is None:
            errors.append(
                KeymapError(
                    "unknown action",
                    f"{action_id!r} is not a bindable action "
                    f"(known: {', '.join(sorted(by_id))})",
                )
            )
            continue
        keys = _binding_keys(action, value, errors)
        if keys is None:
            continue
        for key in keys:
            holder = seen_keys.get(key)
            if holder is not None:
                errors.append(
                    KeymapError(
                        "key collision",
                        f"{key} is bound by both {holder} and {action.action_id}",
                    )
                )
            else:
                seen_keys[key] = action.action_id
    # A user key may not take a reserved key even by binding an innocent
    # action to it: the reserved keys are the interrupt and the exit.
    for action in ACTIONS:
        if not action.reserved:
            continue
        for key, _purpose in action.default_keys:
            holder = seen_keys.get(key)
            if holder is not None and holder != action.action_id:
                errors.append(
                    KeymapError(
                        "reserved key taken",
                        f"{key} is reserved for {action.action_id} and cannot be "
                        f"taken by {holder}: {action.reserved_reason}",
                    )
                )
    return errors


# ---------------------------------------------------------------------------
# The resolved keymap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Keymap:
    """The effective keymap: shipped defaults overlaid with user bindings.

    Every surface projection below reads THIS object. That is the whole
    point: the palette, ``/help`` and the TUI cannot drift from one another
    because they are three projections of one map.
    """

    #: ``action_id -> (canonical keys, ...)``. An empty tuple means the user
    #: deliberately unbound a non-reserved action.
    bindings: Mapping[str, Tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_BINDINGS)
    )

    def keys_for(self, action_id: str) -> Tuple[str, ...]:
        """The keys bound to one action, in display order."""
        return tuple(self.bindings.get(action_id, ()))

    def action_purpose(self, action_id: str) -> str:
        """The action's own purpose sentence (its first default key's)."""
        for action in ACTIONS:
            if action.action_id == action_id:
                return action.default_keys[0][1] if action.default_keys else ""
        return ""

    def key_purpose(self, key: str) -> str:
        """The purpose of one bound key, falling back to the action's own."""
        for action in ACTIONS:
            keys = self.keys_for(action.action_id)
            if key not in keys:
                continue
            for default_key, purpose in action.default_keys:
                if default_key == key:
                    return purpose
            return self.action_purpose(action.action_id)
        return ""

    # -- the three surface projections -------------------------------------

    def command_shortcuts(self) -> Dict[str, Tuple[str, ...]]:
        """``{command name: (keys, ...)}`` — what ``CommandSpec.shortcuts`` consumes."""
        return {
            action.name: self.keys_for(action.action_id)
            for action in ACTIONS
            if action.group == "command"
        }

    def shortcut_label(self, command: str) -> str:
        """The palette/``/help`` cell for one command: ``"ctrl+g / ctrl+b"``.

        An unbound command renders ``""`` — the surfaces already render that
        as an empty shortcut column rather than a blank row.
        """
        return " / ".join(self.command_shortcuts().get(command, ()))

    palette_shortcut = shortcut_label
    help_shortcut = shortcut_label

    def tui_binding_rows(self) -> Tuple[Tuple[str, str, str], ...]:
        """``(key, action_id, purpose)`` per bound key — what ``BINDINGS`` consumes."""
        rows: List[Tuple[str, str, str]] = []
        for action in ACTIONS:
            for key in self.keys_for(action.action_id):
                rows.append((key, action.action_id, self.key_purpose(key)))
        return tuple(rows)

    def keyboard_shortcuts(self) -> Dict[str, List[Dict[str, Any]]]:
        """The exact shape ``cli.commands.keyboard_shortcuts`` returns today.

        Pinned byte-for-byte equal to that function's output on the default
        keymap by ``tests/test_shared_keymap.py`` — so swapping the CLI to
        read this projection is a no-op by construction.
        """
        commands: List[Dict[str, Any]] = []
        for action in ACTIONS:
            if action.group != "command":
                continue
            keys = list(self.keys_for(action.action_id))
            if not keys:
                continue
            commands.append(
                {
                    "command": action.name,
                    "keys": keys,
                    "summary": action.summary,
                    "label": " / ".join(keys),
                    "purposes": [self.key_purpose(key) for key in keys],
                }
            )
        surface = [
            {"key": key, "surface": action.name, "purpose": self.key_purpose(key)}
            for action in ACTIONS
            if action.group == "surface"
            for key in self.keys_for(action.action_id)
        ]
        navigation = [
            {"key": key, "purpose": self.key_purpose(key)}
            for action in ACTIONS
            if action.group == "navigation"
            for key in self.keys_for(action.action_id)
        ]
        return {"commands": commands, "surface": surface, "navigation": navigation}


def resolve_keymap(
    user_bindings: Optional[Mapping[str, Sequence[str]]] = None,
) -> Keymap:
    """Overlay validated user bindings on the shipped defaults.

    Assumes ``user_bindings`` has already passed
    :func:`validate_user_document`; invalid entries are dropped rather than
    applied, so a caller that skipped validation degrades toward defaults —
    the fail-closed direction.
    """
    merged: Dict[str, Tuple[str, ...]] = dict(DEFAULT_BINDINGS)
    for action_id, keys in (user_bindings or {}).items():
        if action_id not in ACTION_IDS:
            continue
        merged[str(action_id)] = tuple(str(key) for key in keys)
    return Keymap(merged)


def keymap_invariant_findings(keymap: Keymap) -> List[str]:
    """The drift-gate invariants over ANY resolved keymap. ``[]`` means clean.

    These are the properties the two-direction gate exists to hold, stated
    over the effective map rather than over the defaults, so a user binding
    is checked by the same rules the shipped one is.
    """
    findings: List[str] = []
    seen: Dict[str, str] = {}
    for action in ACTIONS:
        keys = keymap.keys_for(action.action_id)
        if action.reserved and keys != DEFAULT_BINDINGS[action.action_id]:
            findings.append(
                f"reserved action {action.action_id} lost its default key(s)"
            )
        for key in keys:
            canonical, error = normalize_key(key)
            if error:
                findings.append(f"{action.action_id}: {error}")
                continue
            if canonical != key:
                findings.append(f"{action.action_id}: {key} is not canonical")
            holder = seen.get(key)
            if holder is not None:
                findings.append(
                    f"{key} is bound by both {holder} and {action.action_id}"
                )
            seen[key] = action.action_id
    return findings


# ---------------------------------------------------------------------------
# Loading — fail closed, never raise
# ---------------------------------------------------------------------------


def default_keybindings_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The user keymap's home: beside the global settings file.

    ``NEO_CONFIG``'s parent wins outright, then ``NEO_HOME``/``HARNESS_HOME``
    (test isolation), then the platform config dir — the same precedence
    ``cli.neoconfig.global_settings_path`` uses, restated here because
    ``shared/`` may not import ``cli/``.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    neo_config = str(source.get("NEO_CONFIG") or "").strip()
    if neo_config:
        return Path(neo_config).expanduser().parent / KEYBINDINGS_FILENAME
    for name in ("NEO_HOME", "HARNESS_HOME"):
        value = str(source.get(name) or "").strip()
        if value:
            return Path(value).expanduser() / KEYBINDINGS_FILENAME
    if os.name == "nt":
        base = str(source.get("APPDATA") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = str(source.get("XDG_CONFIG_HOME") or "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "neo" / KEYBINDINGS_FILENAME


def load_user_keymap(
    path: Any,
) -> Tuple[Dict[str, Tuple[str, ...]], List[KeymapError]]:
    """Read and validate a ``keybindings.json``. Never raises.

    Returns ``(bindings, errors)``. A missing file is ``({}, [])`` — no
    opinion. Anything else wrong rejects the WHOLE document: ``({}, [..])``.
    """
    raw_path = Path(path)
    if not raw_path.is_file():
        return {}, []
    try:
        text = raw_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {}, [KeymapError("unreadable keymap", f"{raw_path}: {exc}")]
    try:
        document = json.loads(text)
    except ValueError as exc:
        return {}, [KeymapError("unparseable keymap", f"{raw_path}: {exc}")]
    errors = validate_user_document(document)
    if errors:
        return {}, errors
    bindings = document.get("bindings") or {}
    normalized: Dict[str, Tuple[str, ...]] = {}
    by_id = {action.action_id: action for action in ACTIONS}
    for action_id, value in bindings.items():
        keys = _binding_keys(by_id[str(action_id)], value, [])
        if keys is not None:
            normalized[str(action_id)] = keys
    return normalized, []


@dataclass(frozen=True)
class KeymapState:
    """What a surface needs at startup: the effective keymap plus what went
    wrong reading the user's file. ``source`` says where the bindings came
    from: ``"default"``, ``"user"``, or ``"default+errors"``."""

    keymap: Keymap
    errors: Tuple[str, ...] = ()
    source: str = "default"

    @property
    def ok(self) -> bool:
        """Whether the user's file was fully applied."""
        return self.source == "user" or (self.source == "default" and not self.errors)


def load_keymap_state(path: Any = None) -> KeymapState:
    """The fail-closed entry point a surface calls ONCE at startup.

    NEVER raises: a corrupt, unreadable or invalid keymap yields the default
    keymap with the errors attached, so the TUI starts with the shipped
    bindings and can surface the error — the same reasoning as the import
    smoke test: a single bad character once took the whole CLI offline.
    """
    resolved = Path(path) if path is not None else default_keybindings_path()
    try:
        bindings, errors = load_user_keymap(resolved)
        if errors:
            return KeymapState(
                Keymap(),
                tuple(error.render() for error in errors),
                "default+errors",
            )
        if not bindings:
            return KeymapState(Keymap(), (), "default")
        return KeymapState(resolve_keymap(bindings), (), "user")
    except Exception as exc:  # pragma: no cover - defence in depth
        return KeymapState(Keymap(), (f"keymap unavailable: {exc}",), "default+errors")
