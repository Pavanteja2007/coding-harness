"""The nine persisted UI toggles, and the one store they live in
(VEX-PF-04 — toggles, role hierarchy, inline undo).

Nine knobs decide whether the interface is readable at a glance:

* is the model's thinking shown, or summarised away;
* are tool details expanded or folded to one line;
* is assistant metadata (step, model, tokens) drawn at all;
* do rows carry a timestamp;
* is the sidebar forced, auto, or off;
* is the scrollbar drawn;
* do animations run;
* is code concealed behind a placeholder;
* is the user named.

Every one of them has a **keybind** and a **command**, declared in ONE
table (:data:`TOGGLE_SPECS`) so the footer hint, the palette, ``/help``,
and the widget that honours the toggle cannot disagree about what the
knob is called or what it starts as.

Why this module exists rather than living in ``cli/tui.py``/``cli/
commands.py``: those are other terminals' files. The registry, the
defaults, the resolution order and the persistence are a self-contained
piece of product policy, so it is built here and the *mount points* are
handed over in ``cli/AGENTS.md``. Nothing here imports Textual, so the
table is testable on a host with no terminal at all.

Three decisions that are load-bearing:

1. **The nine defaults are exactly the ones the product round asked
   for** (:data:`TOGGLE_DEFAULTS`), and they are pinned by a test that
   reads this table. A default that drifts is a change of product
   nobody voted for.
2. **These are NOT ``harness/config.py::DEFAULTS`` keys.** A value in
   ``DEFAULTS`` is merged into every ``Task`` and every eval arm, so a
   ``ui_sidebar = "auto"`` row there would silently change every run in
   the project. A knob this round needs to be *readable* from
   ``Task.config`` when a caller passes one (key-PRESENCE, so a typo
   cannot enable it) and persisted in this store otherwise —
   :func:`toggles_from_config` is that seam.
3. **Persistence is fail-closed and out of the user's repository.** A
   corrupt store is reported and the DEFAULTS are used; it is never
   silently half-applied, and it is never rewritten in place. Scope
   files live under the Vex home keyed by repository, so toggling the
   sidebar cannot dirty a checkout.

Nothing here renders: the widgets in :mod:`cli.tui_components` read
these values and produce ``rich.text.Text``, never a markup string.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: Bumped when a stored document's shape changes. A document with a
#: different version is refused rather than half-read.
STORE_VERSION = 1

#: How many entries a persisted store may carry. Nine is the real
#: number; the bound exists so a hand-edited file cannot become an
#: unbounded read on the render path.
MAX_STORED_TOGGLES = 32

#: The closed vocabulary of toggle names. A name outside this set is a
#: programming error, not a user preference, and :func:`set_toggle`
#: refuses it.
TOGGLE_NAMES: Tuple[str, ...] = (
    "thinking_visibility",
    "tool_details_visibility",
    "assistant_metadata_visibility",
    "timestamps",
    "sidebar",
    "scrollbar_visible",
    "animations_enabled",
    "code_conceal",
    "username_visible",
)

#: The tri-state a non-boolean toggle accepts. ``auto`` means "follow
#: the layout policy" and is deliberately NOT the same as either
#: answer: a rail that collapses at 80 columns is the policy working,
#: not the user having chosen something.
#:
#: **THIS IS THE FALLBACK ONLY, and it carries the SAME SPELLINGS the
#: layout authority uses.** It used to be ``("auto", "shown", "hidden")``
#: while ``ToggleSpec.coerce`` canonicalised to ``auto``/``show``/``hide``
#: - two vocabularies for one control, and the seam between them was a
#: ``cycle.index(current)`` that raised or returned 0 depending on which
#: side of it a value came from. Both sides now speak ``show``/``hide``;
#: ``shown``/``hidden`` survive ONLY in
#: :data:`TOGGLE_VALUE_ALIASES`, as accepted spellings that are never
#: written, so a hand-edited store keeps loading and a reader never has to
#: know which one the shell preferred.
#:
#: Where a specific toggle has its own declared vocabulary (``sidebar`` does)
#: that vocabulary wins; this tuple is what a future non-boolean toggle with
#: no row of its own inherits.
TRISTATE_VALUES: Tuple[str, ...] = ("auto", "show", "hide")

#: The shell's sidebar vocabulary is ``cli.design.SIDEBAR_MODES`` and its
#: own alias table. Both are imported when available so the two sides of
#: the boundary cannot disagree about what "hide" is spelled, and both
#: fall back to the literals above when the layout module is not
#: importable. An UNRECOGNISED value is still refused by
#: :meth:`ToggleSpec.coerce` - importing the shell's aliases must not
#: turn this into "accept whatever".
try:  # pragma: no cover - the fallback is the hard-to-reach branch
    from cli.design import (  # type: ignore[attr-defined]
        SIDEBAR_MODE_ALIASES as _DESIGN_SIDEBAR_ALIASES,
    )
    from cli.design import (
        SIDEBAR_MODES as _DESIGN_SIDEBAR_MODES,  # type: ignore[attr-defined]
    )
except Exception:  # pragma: no cover - defensive
    _DESIGN_SIDEBAR_MODES = None
    _DESIGN_SIDEBAR_ALIASES = {}

#: The declared vocabulary PER TOGGLE. A non-boolean toggle with no row
#: here uses :data:`TRISTATE_VALUES`.
TOGGLE_VALUES: Dict[str, Tuple[str, ...]] = {}
#: Spellings accepted but never written, mapped onto the declared one.
TOGGLE_VALUE_ALIASES: Dict[str, str] = {}

if _DESIGN_SIDEBAR_MODES:
    TOGGLE_VALUES["sidebar"] = tuple(_DESIGN_SIDEBAR_MODES)
    TOGGLE_VALUE_ALIASES.update(
        {str(key): str(value) for key, value in _DESIGN_SIDEBAR_ALIASES.items()}
    )
    # The generic spellings stay accepted so a store written before the
    # shell adopted ``cli.design`` still loads instead of silently
    # reverting to the default.
    TOGGLE_VALUE_ALIASES.setdefault("shown", "show")
    TOGGLE_VALUE_ALIASES.setdefault("hidden", "hide")
else:  # pragma: no cover - defensive
    # The SAME three spellings as the literal above. The old fallback was
    # `("auto", "shown", "hidden")`, which reintroduced the second vocabulary
    # on exactly the host where the layout module could not be imported - the
    # one host where nothing would have caught the two disagreeing.
    TOGGLE_VALUES["sidebar"] = TRISTATE_VALUES

#: Every non-boolean toggle and the vocabulary it accepts.
TOGGLE_KINDS: Dict[str, str] = {
    "thinking_visibility": "bool",
    "tool_details_visibility": "bool",
    "assistant_metadata_visibility": "bool",
    "timestamps": "bool",
    "sidebar": "tristate",
    "scrollbar_visible": "bool",
    "animations_enabled": "bool",
    "code_conceal": "bool",
    "username_visible": "bool",
}

#: THE defaults. This table is the contract; the test reads it rather
#: than repeating the values, so a drift fails loudly instead of being
#: agreed to in two places.
TOGGLE_DEFAULTS: Dict[str, Any] = {
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

#: The keybinds. ``ctrl+1`` … ``ctrl+9`` are used because they are
#: nine adjacent, memorable keys that collide with nothing the shell
#: already binds (ctrl+p palette, ctrl+r history, ctrl+y copy, ctrl+q
#: quit, ctrl+x cancel, ctrl+g/ctrl+b steering, ctrl+space mention
#: completion, shift+arrows scrollback). ctrl+m, ctrl+i, ctrl+s, ctrl+j
#: and ctrl+h are deliberately NOT used: they are Enter, Tab, XOFF,
#: LF and Backspace on most terminals, and a keybind a user cannot
#: press is a keybind that does not exist.
TOGGLE_KEYS: Dict[str, str] = {
    "thinking_visibility": "ctrl+1",
    "tool_details_visibility": "ctrl+2",
    "assistant_metadata_visibility": "ctrl+3",
    "timestamps": "ctrl+4",
    "sidebar": "ctrl+5",
    "scrollbar_visible": "ctrl+6",
    "animations_enabled": "ctrl+7",
    "code_conceal": "ctrl+8",
    "username_visible": "ctrl+9",
}

#: The command for each toggle. One command per toggle, named after the
#: knob, so ``/help`` and the palette can teach all nine without a
#: second table.
TOGGLE_COMMANDS: Dict[str, str] = {
    "thinking_visibility": "/thinking",
    "tool_details_visibility": "/details",
    "assistant_metadata_visibility": "/meta",
    "timestamps": "/timestamps",
    "sidebar": "/sidebar",
    "scrollbar_visible": "/scrollbar",
    "animations_enabled": "/animations",
    "code_conceal": "/conceal",
    "username_visible": "/username",
}

#: What the value MEANS in a footer hint, per non-boolean vocabulary.
#: A boolean is rendered from the value, so this only carries the words
#: a bare value cannot. A value with no word here renders as itself, so
#: a new vocabulary member is readable the day it is added rather than
#: blank.
TOGGLE_WORDS: Dict[str, Dict[str, str]] = {
    "sidebar": {
        "auto": "auto",
        "show": "always",
        "shown": "always",
        "hide": "off",
        "hidden": "off",
    },
    "timestamps": {True: "on", False: "off"},
}

#: The action prefix a shell uses when it MOUNTS one of these toggles. A
#: binding already on one of our keys is therefore a mount rather than a
#: conflict, which is the difference between "Prompt 01 wired this" and
#: "two handlers now fight over one keystroke".
MOUNT_ACTION_PREFIX = "toggle_"


def toggle_mounts(bound: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Where each toggle is mounted, and which ones are not.

    ``bound`` is ``{key: action}`` as the shell reports it. A row comes
    back with ``status`` one of:

    * ``"mounted"`` - the shell already binds this key to a toggle
      action, so the registry and the shell agree and nothing is needed.
    * ``"pending"`` - the key is unbound. This is the work the mounting
      terminal still has to do, and it is the list that goes in a
      handoff.
    * ``"conflict"`` - the key is bound to something that is NOT a
      toggle action. Two handlers on one keystroke is a defect nobody
      can debug from a screenshot, so it is reported rather than
      silently overwritten.
    """
    table = dict(bound or {})
    rows: List[Dict[str, Any]] = []
    for spec in TOGGLE_SPECS:
        action = str(table.get(spec.key) or "")
        if not action:
            status = "pending"
        elif action.startswith(MOUNT_ACTION_PREFIX):
            status = "mounted"
        else:
            status = "conflict"
        rows.append(
            {
                "name": spec.name,
                "key": spec.key,
                "command": spec.command,
                "shell_action": action,
                "status": status,
            }
        )
    return rows

#: The anti-clutter rule, declared once so every surface obeys the same
#: threshold. A section with two or fewer entries is not rendered: a
#: two-row panel is a heading plus one fact, and it costs the reader
#: more than it gives.
#:
#: The NUMBER is delegated to `cli.design.ANTI_CLUTTER_MIN_ENTRIES` when
#: that module is importable, because it is the product's single layout
#: authority and two thresholds that agree today diverge tomorrow - and
#: when they diverge, the rail and the panel disagree about what
#: "cluttered" means. The literal fallback keeps this module importable
#: on a host where the shell's layout module is unavailable, which is
#: the same reason this module imports nothing else from the package.
try:  # pragma: no cover - the fallback is the hard-to-reach branch
    from cli.design import ANTI_CLUTTER_MIN_ENTRIES as _DESIGN_MIN_SECTION_ENTRIES
except Exception:  # pragma: no cover - defensive
    _DESIGN_MIN_SECTION_ENTRIES = 3

MIN_SECTION_ENTRIES = int(_DESIGN_MIN_SECTION_ENTRIES)


@dataclass(frozen=True)
class ToggleSpec:
    """One toggle, declared once and read by every surface."""

    name: str
    kind: str
    default: Any
    key: str
    command: str
    label: str
    summary: str

    @property
    def is_boolean(self) -> bool:
        return self.kind == "bool"

    @property
    def values(self) -> Tuple[str, ...]:
        """The declared value vocabulary for a non-boolean toggle."""
        return TOGGLE_VALUES.get(self.name, TRISTATE_VALUES)

    def word(self, value: Any = None) -> str:
        """The plain word for a value. Never raises; a bad value says so."""
        shown = self.default if value is None else value
        if self.is_boolean:
            if isinstance(shown, bool):
                return "on" if shown else "off"
            return f"unusable ({shown!r})"
        return TOGGLE_WORDS.get(self.name, {}).get(str(shown), str(shown))

    def accepts(self, value: Any) -> bool:
        """Whether ``value`` is a legal value for this toggle."""
        if self.is_boolean:
            return isinstance(value, bool)
        if not isinstance(value, str):
            return False
        return value in self.values or value in TOGGLE_VALUE_ALIASES

    def coerce(self, value: Any) -> Optional[Any]:
        """Return the CANONICAL legal value for ``value``, or ``None``.

        Deliberately narrow: a JSON store holds real booleans and real
        strings, and a string ``"false"`` is NOT accepted as a boolean.
        A toggle that silently read ``"false"`` as true (because a
        non-empty string is truthy) is the same defect class as a
        verifier that reports unverified work as verified.

        An accepted alias is rewritten to the vocabulary the product
        declares, so what lands on disk is one spelling and a reader
        never has to know which one the shell preferred.
        """
        if self.is_boolean:
            return value if isinstance(value, bool) else None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text in self.values:
            return text
        alias = TOGGLE_VALUE_ALIASES.get(text.lower())
        if alias in self.values:
            return alias
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "default": self.default,
            "key": self.key,
            "command": self.command,
            "label": self.label,
            "summary": self.summary,
        }


def _spec(
    name: str,
    label: str,
    summary: str,
    *,
    key: str,
    command: str,
) -> ToggleSpec:
    return ToggleSpec(
        name=name,
        kind=TOGGLE_KINDS[name],
        default=TOGGLE_DEFAULTS[name],
        key=key,
        command=command,
        label=label,
        summary=summary,
    )


#: The one table. Ordered so ``/help`` and the palette teach them in the
#: order a reader meets them: what the model is doing, then what it
#: did, then what the window is.
TOGGLE_SPECS: Tuple[ToggleSpec, ...] = (
    _spec(
        "thinking_visibility",
        "thinking",
        "show or fold the model's reasoning",
        key=TOGGLE_KEYS["thinking_visibility"],
        command=TOGGLE_COMMANDS["thinking_visibility"],
    ),
    _spec(
        "tool_details_visibility",
        "tool details",
        "expand tool output or fold it to one line",
        key=TOGGLE_KEYS["tool_details_visibility"],
        command=TOGGLE_COMMANDS["tool_details_visibility"],
    ),
    _spec(
        "assistant_metadata_visibility",
        "metadata",
        "draw step, model and token metadata",
        key=TOGGLE_KEYS["assistant_metadata_visibility"],
        command=TOGGLE_COMMANDS["assistant_metadata_visibility"],
    ),
    _spec(
        "timestamps",
        "timestamps",
        "print the time on every row",
        key=TOGGLE_KEYS["timestamps"],
        command=TOGGLE_COMMANDS["timestamps"],
    ),
    _spec(
        "sidebar",
        "sidebar",
        "force the rail shown or hidden, or follow the layout",
        key=TOGGLE_KEYS["sidebar"],
        command=TOGGLE_COMMANDS["sidebar"],
    ),
    _spec(
        "scrollbar_visible",
        "scrollbar",
        "draw the transcript scrollbar",
        key=TOGGLE_KEYS["scrollbar_visible"],
        command=TOGGLE_COMMANDS["scrollbar_visible"],
    ),
    _spec(
        "animations_enabled",
        "animations",
        "run spinner and transition motion",
        key=TOGGLE_KEYS["animations_enabled"],
        command=TOGGLE_COMMANDS["animations_enabled"],
    ),
    _spec(
        "code_conceal",
        "code conceal",
        "collapse long code bodies behind a marker",
        key=TOGGLE_KEYS["code_conceal"],
        command=TOGGLE_COMMANDS["code_conceal"],
    ),
    _spec(
        "username_visible",
        "username",
        "show who a message is from",
        key=TOGGLE_KEYS["username_visible"],
        command=TOGGLE_COMMANDS["username_visible"],
    ),
)

#: name -> spec, for the O(1) lookups a frame path does.
TOGGLE_BY_NAME: Dict[str, ToggleSpec] = {spec.name: spec for spec in TOGGLE_SPECS}

#: keybind -> toggle, for a surface that wants to render every binding.
TOGGLE_BY_KEY: Dict[str, ToggleSpec] = {spec.key: spec for spec in TOGGLE_SPECS}

#: command -> toggle.
TOGGLE_BY_COMMAND: Dict[str, ToggleSpec] = {
    spec.command: spec for spec in TOGGLE_SPECS
}


def toggle_spec(name: Any) -> Optional[ToggleSpec]:
    """Return the declared spec for ``name``, or ``None`` if unknown."""
    return TOGGLE_BY_NAME.get(str(name or ""))


def default_toggles() -> Dict[str, Any]:
    """A fresh copy of the defaults. Never the shared dict."""
    return dict(TOGGLE_DEFAULTS)


def _validate_registry() -> None:
    """Fail at import if the table disagrees with itself.

    A typo in a keybind, a command, or a name would otherwise surface
    as "the shortcut does nothing", which is indistinguishable from a
    terminal that dropped the key.
    """
    names = tuple(spec.name for spec in TOGGLE_SPECS)
    if names != TOGGLE_NAMES:
        raise ValueError(f"TOGGLE_SPECS does not match TOGGLE_NAMES: {names!r}")
    if set(TOGGLE_DEFAULTS) != set(TOGGLE_NAMES):
        raise ValueError("TOGGLE_DEFAULTS does not cover every toggle")
    if set(TOGGLE_KEYS) != set(TOGGLE_NAMES):
        raise ValueError("TOGGLE_KEYS does not cover every toggle")
    if set(TOGGLE_COMMANDS) != set(TOGGLE_NAMES):
        raise ValueError("TOGGLE_COMMANDS does not cover every toggle")
    if len(set(TOGGLE_KEYS.values())) != len(TOGGLE_NAMES):
        raise ValueError("two toggles share one keybind")
    if len(set(TOGGLE_COMMANDS.values())) != len(TOGGLE_NAMES):
        raise ValueError("two toggles share one command")
    for spec in TOGGLE_SPECS:
        if not spec.accepts(spec.default):
            raise ValueError(f"{spec.name} has an unusable default: {spec.default!r}")
    for name, values in TOGGLE_VALUES.items():
        if not values:
            raise ValueError(f"{name} declares an empty value vocabulary")


_validate_registry()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: Any) -> str:
    """A filesystem-safe token for an arbitrary identifier."""
    return _UNSAFE.sub("-", str(value or "").strip()) or "default"


def repo_key(repo_path: Any) -> str:
    """A stable, filesystem-safe key for a repository path.

    The same absolute path always produces the same key, and two
    different repositories never share one. Case is normalised so
    ``C:\\Repo`` and ``c:\\repo`` are the same repository on a
    case-insensitive filesystem.
    """
    text = str(repo_path or "").strip()
    if not text:
        return "no-repo"
    try:
        resolved = str(Path(text).expanduser().resolve())
    except Exception:  # pragma: no cover - defensive
        resolved = text
    try:
        resolved = str(Path(resolved).resolve())
    except Exception:  # pragma: no cover - defensive
        pass
    folded = os.path.normcase(resolved)
    digest = hashlib.sha256(folded.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{_slug(Path(folded).name or 'repo')}-{digest}"


def vex_home(home: Any = None) -> Path:
    """The Vex home that owns UI preference state.

    ``VEX_HOME`` wins so a test run never marks the developer's real
    machine's preferences as changed. Falls back to the platform data
    directory via the stdlib rather than importing a sibling module,
    so this table is importable with nothing else present.
    """
    override = str(home or os.environ.get("VEX_HOME") or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "vex"
        return Path.home() / "AppData" / "Local" / "vex"
    base = os.environ.get("XDG_DATA_HOME") or ""
    if base:
        return Path(base) / "vex"
    return Path.home() / ".local" / "share" / "vex"


def repo_toggle_path(repo_path: Any, *, home: Any = None) -> Path:
    """Where the per-repository toggle document lives."""
    return vex_home(home) / "ui" / "toggles" / f"{repo_key(repo_path)}.json"


def session_toggle_path(session_id: Any, *, home: Any = None) -> Path:
    """Where the per-session toggle document lives."""
    return vex_home(home) / "ui" / "sessions" / f"{_slug(session_id)}.json"


def read_store(path: Any) -> Tuple[Dict[str, Any], str]:
    """Read one toggle document. Returns ``(values, note)``.

    ``note`` is non-empty whenever anything about the document was
    refused, and the returned values are then whatever COULD be read
    legally — never a partially-understood value silently applied. A
    missing file is the normal case, not a warning.
    """
    try:
        target = Path(path)
    except Exception:  # pragma: no cover - defensive
        return ({}, "unusable path")
    try:
        if not target.is_file():
            return ({}, "")
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ({}, f"unreadable: {exc.__class__.__name__}")
    try:
        document = json.loads(raw)
    except Exception:
        return ({}, "not valid JSON: the defaults are in force")
    if not isinstance(document, Mapping):
        return ({}, "not a document: the defaults are in force")
    if document.get("version") != STORE_VERSION:
        return ({}, f"unsupported store version {document.get('version')!r}")
    stored = document.get("toggles")
    if not isinstance(stored, Mapping):
        return ({}, "no toggles in the document: the defaults are in force")
    if len(stored) > MAX_STORED_TOGGLES:
        return ({}, f"store holds more than {MAX_STORED_TOGGLES} entries")
    values: Dict[str, Any] = {}
    refused: List[str] = []
    for name in TOGGLE_NAMES:
        if name not in stored:
            continue
        spec = TOGGLE_BY_NAME[name]
        coerced = spec.coerce(stored[name])
        if coerced is None:
            refused.append(name)
            continue
        values[name] = coerced
    unknown = [str(key) for key in stored if str(key) not in TOGGLE_BY_NAME]
    notes: List[str] = []
    if refused:
        notes.append("unusable value refused for: " + ", ".join(sorted(refused)))
    if unknown:
        notes.append("unknown toggle ignored: " + ", ".join(sorted(unknown)))
    return (values, "; ".join(notes))


def write_store(path: Any, values: Mapping[str, Any]) -> Tuple[bool, str]:
    """Write one toggle document atomically. Returns ``(written, note)``.

    Values are filtered through :meth:`ToggleSpec.coerce` here, not
    only on read, so a file on disk can never hold a value the reader
    would refuse. A write failure is REPORTED, never swallowed: a
    preference that looks saved and was not is the same defect class
    as a verification that looks passed and was not.
    """
    try:
        target = Path(path)
    except Exception:  # pragma: no cover - defensive
        return (False, "unusable path")
    clean: Dict[str, Any] = {}
    for name in TOGGLE_NAMES:
        if name not in (values or {}):
            continue
        coerced = TOGGLE_BY_NAME[name].coerce((values or {})[name])
        if coerced is not None:
            clean[name] = coerced
    if not clean:
        return (False, "nothing legal to write")
    document = {
        "version": STORE_VERSION,
        "toggles": {name: clean[name] for name in TOGGLE_NAMES if name in clean},
    }
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError as exc:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
        return (False, f"could not write: {exc.__class__.__name__}")
    return (True, "")


@dataclass
class ToggleSettings:
    """The resolved toggles for one surface, and where each value came from.

    Resolution order is **session > repository > default**, and every
    value records its own source so a receipt can say *why* a knob is
    where it is. A value that is in neither store reports its default
    and says so, because "the default" and "the user's saved choice"
    are the same bytes with completely different meanings.
    """

    values: Dict[str, Any] = field(default_factory=default_toggles)
    sources: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    repo_path: Any = None
    session_id: Any = None
    #: The Vex home these settings resolve their stores against. Carried on
    #: the object, not recomputed per write: a caller that passed an
    #: explicit home (a test, a sandboxed run) must not have its write
    #: land in the developer's real preferences because the flush forgot.
    home: Any = None
    _repo_dirty: Dict[str, Any] = field(default_factory=dict, repr=False)
    _session_dirty: Dict[str, Any] = field(default_factory=dict, repr=False)

    # -- reads

    def get(self, name: Any, default: Any = None) -> Any:
        """The value of a toggle, or ``default`` for an unknown name."""
        key = str(name or "")
        if key in self.values:
            return self.values[key]
        return TOGGLE_DEFAULTS.get(key, default)

    def source(self, name: Any) -> str:
        """Where a value came from: default / repo / session / unknown."""
        return self.sources.get(str(name or ""), "unknown")

    def is_true(self, name: Any) -> bool:
        """Truthiness of a toggle, resolving a non-boolean default safely."""
        value = self.get(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value not in ("hidden", "off", "false", "no")
        return bool(value)

    def enabled(self, name: Any) -> bool:
        """Alias for :meth:`is_true`; reads better at a call site."""
        return self.is_true(name)

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-safe copy of the resolved values."""
        return {name: self.values.get(name, TOGGLE_DEFAULTS[name]) for name in TOGGLE_NAMES}

    def report(self) -> List[Dict[str, Any]]:
        """One receipt row per toggle, for a hint bar, ``--json`` or a test."""
        rows: List[Dict[str, Any]] = []
        for spec in TOGGLE_SPECS:
            value = self.values.get(spec.name, spec.default)
            rows.append(
                {
                    "name": spec.name,
                    "value": value,
                    "word": spec.word(value),
                    "source": self.source(spec.name),
                    "key": spec.key,
                    "command": spec.command,
                    "label": spec.label,
                }
            )
        return rows

    # -- writes

    def set(
        self, name: Any, value: Any, *, persist: bool = True
    ) -> Tuple[bool, str]:
        """Set one toggle in memory (and, by default, on disk).

        Returns ``(changed, note)``. An unknown name, an unusable
        value, or a refused write all return ``False`` with a reason:
        a toggle that appears to have flipped and did not is worse than
        one that visibly did not move.
        """
        key = str(name or "")
        spec = TOGGLE_BY_NAME.get(key)
        if spec is None:
            return (False, f"unknown toggle: {key or '(empty)'}")
        coerced = spec.coerce(value)
        if coerced is None:
            return (False, f"{key} does not accept {value!r}")
        if coerced == self.values.get(key, spec.default):
            return (False, f"{key} is already {spec.word(coerced)}")
        self.values[key] = coerced
        self.sources[key] = "session"
        self._session_dirty[key] = coerced
        if persist:
            return self.flush()
        return (True, "")

    def flip(self, name: Any, *, persist: bool = True) -> Tuple[bool, str]:
        """Toggle a boolean knob; advance a tri-state knob one step.

        **The advance reads the LIVE value.** The previous line was
        ``coerce_value(key, spec.default)``, which makes ``current`` the
        registry DEFAULT no matter what the store holds. The consequence is
        the stall this method's own history records: ``auto`` indexes to 0,
        so the next value is always ``show``; the first flip succeeds
        (``auto`` -> ``show``) and every flip after it is refused by
        :meth:`set` with "already always", because ``show`` IS the value it
        keeps proposing. Measured on the tree before this fix::

            start  auto   (source: default)
            flip 0  True   ''                  -> show
            flip 1  False  'sidebar is already always'  -> show
            flip 2  False  'sidebar is already always'  -> show
            ...forever

        Note the brief's diagnosis - "it indexes ``TRISTATE_VALUES``
        (``auto``/``shown``/``hidden``) while ``coerce`` canonicalises to
        ``auto``/``show``/``hide``" - is the FORM this defect took when it
        was first recorded and is no longer the cause: ``cycle`` is read from
        ``spec.values``, which resolves through ``TOGGLE_VALUES`` to the
        layout authority's vocabulary. The stall survived the vocabulary fix
        because the bug was never the vocabulary, it was reading the wrong
        variable. Fixing the vocabulary alone would have left the toggle
        dead.
        """
        key = str(name or "")
        spec = TOGGLE_BY_NAME.get(key)
        if spec is None:
            return (False, f"unknown toggle: {key or '(empty)'}")
        if spec.is_boolean:
            return self.set(key, not self.is_true(key), persist=persist)
        current = self.coerce_value(key, self.get(key, spec.default))
        cycle = spec.values
        index = cycle.index(current) if current in cycle else 0
        return self.set(key, cycle[(index + 1) % len(cycle)], persist=persist)

    def coerce_value(self, name: Any, value: Any) -> Any:
        """The canonical value for a stored/typed value, or the default.

        Reading through the same coercion as writing means a value
        accepted from a store, from ``Task.config`` or from a command line
        is normalised identically, so two surfaces cannot report the same
        preference as two different words.
        """
        key = str(name or "")
        spec = TOGGLE_BY_NAME.get(key)
        if spec is None:
            return value
        coerced = spec.coerce(value)
        return spec.default if coerced is None else coerced

    def set_many(
        self, values: Mapping[str, Any], *, persist: bool = True
    ) -> List[Dict[str, Any]]:
        """Apply a mapping, returning one receipt row per name."""
        rows: List[Dict[str, Any]] = []
        for name, value in dict(values or {}).items():
            changed, note = self.set(name, value, persist=False)
            rows.append({"name": str(name), "changed": changed, "note": note})
        if persist:
            wrote, note = self.flush()
            for row in rows:
                row["persisted"] = wrote
                if note:
                    row["note"] = (row["note"] + "; " + note).strip("; ")
        return rows

    def flush(self) -> Tuple[bool, str]:
        """Write both dirty scopes. Returns ``(all_written, note)``.

        Both writes are attempted even when the first fails: losing a
        repository-scoped choice because the session file was locked
        is the same class of silent loss.
        """
        notes: List[str] = []
        written = True
        if self._session_dirty:
            if self.session_id:
                ok, note = write_store(
                    session_toggle_path(self.session_id, home=self.home),
                    self._session_dirty,
                )
            else:
                ok, note = (False, "no session id: the change is not persisted")
            written = written and ok
            if note:
                notes.append(f"session store: {note}")
            else:
                self._session_dirty = {}
        if self._repo_dirty:
            if self.repo_path:
                ok, note = write_store(
                    repo_toggle_path(self.repo_path, home=self.home), self._repo_dirty
                )
            else:
                ok, note = (False, "no repository: the change is not persisted")
            written = written and ok
            if note:
                notes.append(f"repository store: {note}")
            else:
                self._repo_dirty = {}
        return (written, "; ".join(notes))

    def remember_for_repo(self, values: Mapping[str, Any]) -> bool:
        """Stage values for the REPOSITORY scope (not the session)."""
        staged: Dict[str, Any] = {}
        for name, value in dict(values or {}).items():
            key = str(name)
            coerced = TOGGLE_BY_NAME.get(key) and TOGGLE_BY_NAME[key].coerce(value)
            if coerced is not None:
                staged[key] = coerced
                self.values[key] = coerced
                self.sources[key] = "repo"
        if not staged:
            return False
        self._repo_dirty.update(staged)
        return True


def load_settings(
    repo_path: Any = None,
    session_id: Any = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    home: Any = None,
) -> ToggleSettings:
    """Resolve the toggles for a surface. Total, and never raises.

    The order is: ``config`` key-presence (an explicit caller), then
    the session store, then the repository store, then the defaults. A
    value in ``config`` wins because a caller that passed a dict said
    so; a key that is merely PRESENT is honoured, and a present-but-
    unusable value falls through to the store rather than being
    coerced into something the reader would have refused.
    """
    settings = ToggleSettings(
        repo_path=repo_path, session_id=session_id, home=home
    )
    if config:
        for name in TOGGLE_NAMES:
            if name not in config:
                continue
            coerced = TOGGLE_BY_NAME[name].coerce(config[name])
            if coerced is None:
                settings.notes.append(
                    f"config value for {name} is unusable and was ignored"
                )
                continue
            settings.values[name] = coerced
            settings.sources[name] = "config"
    if session_id:
        values, note = read_store(session_toggle_path(session_id, home=home))
        if note:
            settings.notes.append(f"session store: {note}")
        for name, value in values.items():
            if settings.sources.get(name) == "config":
                continue
            settings.values[name] = value
            settings.sources[name] = "session"
    if repo_path:
        values, note = read_store(repo_toggle_path(repo_path, home=home))
        if note:
            settings.notes.append(f"repository store: {note}")
        for name, value in values.items():
            if settings.sources.get(name) in ("config", "session"):
                continue
            settings.values[name] = value
            settings.sources[name] = "repo"
    for name in TOGGLE_NAMES:
        settings.sources.setdefault(name, "default")
    return settings


def toggles_from_config(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The ``Task.config`` seam: a resolved mapping with no persistence.

    A caller that only wants the values (a widget that reads a task's
    config and nothing else) gets a plain dict. Absent keys are
    ABSENT from the result, not filled with the default, so a caller
    can still tell "nobody said" from "somebody said yes" — which is
    exactly the distinction the rest of this module needs.
    """
    resolved: Dict[str, Any] = {}
    for name in TOGGLE_NAMES:
        if not config or name not in config:
            continue
        coerced = TOGGLE_BY_NAME[name].coerce((config or {})[name])
        if coerced is not None:
            resolved[name] = coerced
    return resolved


# ---------------------------------------------------------------------------
# Surfacing
# ---------------------------------------------------------------------------


def toggle_hint_rows(
    settings: ToggleSettings, *, width: int = 100
) -> List[str]:
    """One short plain-text row per toggle, for a hint bar.

    Plain strings, never markup: a repository name or a command
    argument must not be able to close a style tag. Rows are returned
    whole; the caller that has a width decides how to fold them, and
    :func:`hint_line` does the folding for the common case.
    """
    rows = [f"{spec.key} {spec.label} {settings.get(spec.name)}" for spec in TOGGLE_SPECS]
    return [row[: max(8, int(width or 0) or 8)] for row in rows]


def hint_line(
    settings: ToggleSettings, *, names: Optional[Iterable[str]] = None, width: int = 100
) -> str:
    """One line teaching the keybinds, bounded to ``width``.

    Returns the EMPTY STRING when fewer than :data:`MIN_SECTION_ENTRIES`
    toggles are named: a one- or two-row hint band is a decoration, and
    the anti-clutter rule says a section that small is not rendered.
    """
    chosen = [TOGGLE_BY_NAME[str(name)] for name in (names or TOGGLE_NAMES) if str(name) in TOGGLE_BY_NAME]
    if len(chosen) < MIN_SECTION_ENTRIES:
        return ""
    limit = max(8, int(width or 0) or 8)
    parts: List[str] = []
    for spec in chosen:
        parts.append(f"{spec.key} {spec.label} {spec.word(settings.get(spec.name))}")
        joined = " · ".join(parts)
        if len(joined) > limit:
            return " · ".join(parts[:-1]) if len(parts) > 1 else ""
    return " · ".join(parts)


def section_rows(rows: Optional[Iterable[Any]]) -> List[Any]:
    """Apply the anti-clutter rule to a section's rows.

    A section with two or fewer entries is NOT rendered. This is a
    function rather than a convention because the rule is the one that
    every panel in this product is judged by, and a rule that lives in
    five places is a rule that one of them will forget.
    """
    materialised = [row for row in list(rows or []) if row not in (None, "")]
    if len(materialised) < MIN_SECTION_ENTRIES:
        return []
    return materialised


def render_hint_markup_rows(
    settings: ToggleSettings, *, width: int = 100
) -> List[str]:
    """Alias kept for the surface that wants a named entry point."""
    return toggle_hint_rows(settings, width=width)
