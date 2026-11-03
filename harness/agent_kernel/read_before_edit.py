"""Read-before-edit, enforced at the tool, measured rather than described.

**The claim this module exists to make false.** Phase 0 audited
`harness/agent_kernel/subagents.py` and found OpenCode's edit tool description
*"claims enforcement ('This tool will error if you attempt an edit without
reading the file') -- but no runtime read-tracking exists in the tool's source;
the description overclaims, a reminder that prompt text is a behavioral wish,
not a mechanism."*

That is the repo's own recorded lesson, so a tool description that says a thing
is enforced is worth nothing unless something enforces it. This module is that
something: a turn-scoped read ledger, a refusal produced by it, and a
:class:`ReadBeforeEditVerdict` a caller can assert on.

**The rule, and where it comes from.** Mistral Vibe's wording is the strict
form: *"Never edit a file you have not read in this session. Do not edit a file
in the same turn you first read it -- read, then act on the next turn."* Two
distinct refusals, because they are two distinct defects:

``never_read``
    The session has no observation of this file at all. Editing a file you have
    never seen is how a run overwrites content it does not know exists.

``same_turn``
    The session read it **in this turn**. The rule here is the interesting one
    and it is counter-intuitive on its face -- reading the file is normally
    enough. It is not, because a model that reads and edits in the same turn is
    editing against a mental model formed seconds earlier, and a long file read
    is the one place where the read has not finished informing the writer. The
    cost of the rule is one turn; the benefit is that the read is a real
    constraint rather than a checkbox.

**What it is NOT.** This is not the existing ``stale_read`` digest check, and
the two answer different questions. ``stale_read`` asks *"is the content still
what I read?"* and binds a sha256 to catch a concurrent write. This asks *"did
I read it at all, and was it a turn ago?"* A registry can pass ``stale_read``
with a digest it bound itself at dispatch time and never refuse a single edit;
this cannot pass with no read recorded, because there is no digest to bind and
nothing to compare.

**Default OFF, and the measurement that decided it.** ``read_before_edit`` and
``read_before_edit_same_turn`` are config keys, neither is in
``harness/config.py::DEFAULTS``, and both default to off. That is a measured
decision, not caution: R2-06 measured the *weaker* digest requirement
(``require_edit_digest=True``) at **41/52 daily-driver arms** against a
shipped 52/52, because every scripted fixture writes before it reads. A default
that manufactures a false verified success is worse than an absent default --
that is R2-06's recorded finding and it is why ``require_edit_digest`` is
``None`` in ``DEFAULTS`` today. This module is the tool-level enforcement; the
operator turns it on. See ``harness/phase4_measure_design.py`` for the
with/without measurement that decides the default, and the Handoff for the
numbers this round measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The stable slug a read-before-edit refusal carries. A caller (and a test)
#: matches on this rather than parsing prose. Deliberately a NEW slug and not a
#: reuse of ``stale_read``: the two gates answer different questions and a
#: receipt that could not tell them apart could not tell a reader whether the
#: file changed or was never read.
ERROR_READ_BEFORE_EDIT = "read_before_edit_refused"

#: The two reasons, as a closed set.
READ_BEFORE_EDIT_REFUSALS: Tuple[str, ...] = ("never_read", "same_turn")

#: The tools whose calls are gated. This is a deliberate SUBSET of
#: ``_REVISION_BOUND_TOOLS``: ``read`` is obviously not in it, and the whole
#: point of the gate is that a read is always allowed. Callers pass the tool's
#: own decision of whether a call writes, so a new mutating tool is gated by
#: the mechanism rather than needing a list edit.
READ_GATED_SIDE_EFFECT_CLASSES: Tuple[str, ...] = ("workspace_write", "vcs_write")

#: An env-var escape for the two gate keys, read BELOW the caller's config dict.
#:
#: **It exists so the with/without measurement can be RUN without editing
#: ``evals/``, which is another terminal's file.** A measurement that needs a
#: cross-terminal edit to start is a measurement that does not happen, and the
#: R2-06 result (a stricter digest requirement measured 41/52 daily-driver arms)
#: is exactly the kind of number nobody would have if the arm could not be run.
#:
#: It cannot widen anything: an explicit config key always wins, and the env var
#: reaches the same two booleans a config key reaches. The truthy set is closed,
#: so a typo cannot enable a gate -- an unrecognised value is OFF, which is the
#: safe direction because the alternative is a gate a typo turned on.
READ_BEFORE_EDIT_ENV = "NEO_READ_BEFORE_EDIT"
READ_BEFORE_EDIT_SAME_TURN_ENV = "NEO_READ_BEFORE_EDIT_SAME_TURN"
_TRUTHY = frozenset({"1", "true", "yes", "on", "enforce"})

__all__ = [
    "ERROR_READ_BEFORE_EDIT",
    "READ_BEFORE_EDIT_REFUSALS",
    "READ_GATED_SIDE_EFFECT_CLASSES",
    "ReadBeforeEditRefusal",
    "ReadBeforeEditVerdict",
    "TurnReadLedger",
    "check_read_before_edit",
    "read_before_edit_enabled",
    "read_before_edit_report",
]


def _as_turn(value: Any) -> Optional[int]:
    """Coerce a turn marker to a positive int, or ``None`` when absent.

    ``None`` is the honest "we do not know which turn this is" answer, and it
    DISABLES the ``same_turn`` rule rather than defaulting it to turn 0 -- a
    missing turn marker must not manufacture a refusal, and must not silently
    enable one either.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        turn = int(value)
    except (TypeError, ValueError):
        return None
    return turn if turn >= 0 else None


def _armed(value: Any) -> bool:
    """Return whether one config value means ON.

    A string is matched against a CLOSED truthy set, so ``"ture"`` is OFF. A
    non-string uses Python truthiness, which is what a JSON ``true`` needs.
    """
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def _env_armed(primary: str, legacy: str) -> bool:
    """Return whether the env-var measurement seam arms a gate."""
    import os

    for name in (primary, legacy):
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip().lower() in _TRUTHY
    return False


def read_before_edit_enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Return whether the ``never_read`` rule is armed.

    Read by KEY MEANING, never by truthiness of the merged config: a value in
    ``DEFAULTS`` is merged into every task and every eval arm, so the key must
    be opt-in and its absence must be a real OFF arm rather than an accident of
    a default. An unparseable value is OFF, because a gate nobody can turn on is
    not a gate and a gate that turns itself on is worse.

    Resolution order, lowest priority last:

    1. the key present in ``config`` (wins, including an explicit ``False``);
    2. :data:`READ_BEFORE_EDIT_ENV` (and its ``VEX_`` spelling);
    3. OFF.
    """
    if isinstance(config, Mapping) and "read_before_edit" in config:
        return _armed(config.get("read_before_edit"))
    return _env_armed(READ_BEFORE_EDIT_ENV, "VEX_READ_BEFORE_EDIT")


def read_before_edit_same_turn_enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Return whether the stricter same-turn rule is armed.

    Separate from the never-read rule because they are separate defects and
    separate costs, and the brief's requirement is "refused **or** requires an
    explicit acknowledgment" -- a caller may want one without the other.
    """
    if isinstance(config, Mapping) and "read_before_edit_same_turn" in config:
        return _armed(config.get("read_before_edit_same_turn"))
    return _env_armed(READ_BEFORE_EDIT_SAME_TURN_ENV, "VEX_READ_BEFORE_EDIT_SAME_TURN")


class TurnReadLedger:
    """Which turn each path was read on. ONE PER RUN -- never module state.

    A process-global ledger would let one task's read vouch for another task's
    edit, which is the exact defect ``harness/editor.py::EditSession`` was built
    to avoid and the reason its docstring says so. This class holds the same
    discipline: the owner constructs it, and a run that forgot to would have no
    ledger rather than a shared one.

    Three facts per path, because three different questions are asked about it:

    ``first_turn``
        the turn it was FIRST read on -- what ``same_turn`` compares against;
    ``last_turn``
        the turn it was most recently read on -- so a re-read clears the
        same-turn condition without a new session;
    ``reads``
        the count, so a receipt can distinguish "read once" from "read eleven
        times while guessing".

    ``mark_mutated`` records that the run itself changed the path. A file the
    run has demonstrably just written is not "unread" in any useful sense: the
    run knows its post-image because it produced it. This is the same reasoning
    as ``ToolRegistry._refresh_after_mutation``, and it is stated here rather
    than re-derived, because a gate that refuses a run's own follow-up edit
    would be a gate that gets turned off.
    """

    def __init__(self) -> None:
        self._first_turn: Dict[str, int] = {}
        self._last_turn: Dict[str, int] = {}
        self._reads: Dict[str, int] = {}
        self._mutated: Dict[str, str] = {}
        self._current_turn: Optional[int] = None

    # -- the current turn ------------------------------------------------

    def set_turn(self, turn: Any) -> Optional[int]:
        """Record which turn subsequent observations belong to.

        Returns the resolved turn, or ``None`` when the argument was unusable.
        ``None`` is retained rather than defaulted to 0 on purpose: a turn 0
        would make every read look like "turn 0", and the ``same_turn`` rule
        would then fire on the run's very first turn regardless of reality.
        """
        self._current_turn = _as_turn(turn)
        return self._current_turn

    def current_turn(self) -> Optional[int]:
        """Return the turn this ledger is currently on, or ``None``."""
        return self._current_turn

    # -- keys ------------------------------------------------------------

    @staticmethod
    def key(relative_path: Any) -> str:
        """Normalise a path to the ledger's key form.

        Separators are unified and ``./`` prefixes dropped. A ledger keyed on
        ``a/b.py`` and ``./a/b.py`` as two entries would let a gate be satisfied
        by reading one spelling and then edited through the other, which is the
        same hole the digest check has to defend against separately.
        """
        text = str(relative_path or "").replace("\\", "/").strip()
        while text.startswith("./"):
            text = text[2:]
        return text.strip("/")

    # -- recording -------------------------------------------------------

    def note_read(self, relative_path: Any, turn: Any = None) -> str:
        """Record that this session observed ``relative_path`` on ``turn``.

        Returns the key, or ``""`` for an unusable path. An unknown turn is
        recorded with turn ``-1``, which matches no rule: a read whose turn we
        cannot name still COUNTS as a read for ``never_read`` (we know it
        happened) and does NOT count as same-turn (we cannot place it). That
        asymmetry is deliberate -- the safe direction for each question
        independently is not the same direction, so neither is defaulted.
        """
        key = self.key(relative_path)
        if not key:
            return ""
        turn_index = _as_turn(turn)
        recorded = turn_index if turn_index is not None else -1
        if key not in self._first_turn:
            self._first_turn[key] = recorded
        self._last_turn[key] = recorded
        self._reads[key] = self._reads.get(key, 0) + 1
        return key

    def mark_mutated(self, relative_path: Any, *, tool: str = "") -> str:
        """Record that this run changed ``relative_path`` itself."""
        key = self.key(relative_path)
        if not key:
            return ""
        self._mutated[key] = str(tool or "")
        return key

    # -- queries ---------------------------------------------------------

    def was_read(self, relative_path: Any) -> bool:
        """Return whether this session recorded ANY observation of the path."""
        return self.key(relative_path) in self._first_turn

    def was_mutated_by_run(self, relative_path: Any) -> bool:
        """Return whether this run itself changed the path."""
        return self.key(relative_path) in self._mutated

    def first_turn(self, relative_path: Any) -> Optional[int]:
        """Return the turn the path was first read on, or ``None``."""
        return self._first_turn.get(self.key(relative_path))

    def read_count(self, relative_path: Any) -> int:
        """Return how many times the path was read in this session."""
        return int(self._reads.get(self.key(relative_path), 0))

    def paths(self) -> Tuple[str, ...]:
        """Return every path this ledger knows, sorted."""
        return tuple(sorted(set(self._first_turn) | set(self._mutated)))

    def report(self) -> Dict[str, Any]:
        """Return a JSON-compatible summary, for a run receipt."""
        return {
            "read_paths": len(self._first_turn),
            "mutated_paths": len(self._mutated),
            "total_reads": sum(self._reads.values()),
            "paths": [
                {
                    "path": key,
                    "first_turn": self._first_turn.get(key),
                    "last_turn": self._last_turn.get(key),
                    "reads": int(self._reads.get(key, 0)),
                    "mutated_by_run": str(self._mutated.get(key, "")),
                }
                for key in sorted(set(self._first_turn) | set(self._mutated))
            ],
        }

    def __len__(self) -> int:
        """Return how many distinct paths the ledger knows."""
        return len(set(self._first_turn) | set(self._mutated))


@dataclass(frozen=True)
class ReadBeforeEditRefusal:
    """One refused path, with the reason and the escape hatch named."""

    path: str
    reason: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Return whether this is a pass. A refusal is never a pass."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this refusal."""
        return {"path": self.path, "reason": self.reason, "detail": self.detail}

    def render(self) -> str:
        """Return the one model-facing line naming the escape hatch."""
        if self.reason == "never_read":
            return (
                f"TOOL ERROR [{ERROR_READ_BEFORE_EDIT}]: {self.path} was never read "
                f"in this session. {self.detail} Read it first, then edit it on the "
                "next turn."
            )
        return (
            f"TOOL ERROR [{ERROR_READ_BEFORE_EDIT}]: {self.path} was first read on "
            f"THIS turn. {self.detail} Read it, take the turn's tool result, and "
            "edit it on the next turn -- or pass acknowledge=... with a reason to "
            "override deliberately."
        )


@dataclass(frozen=True)
class ReadBeforeEditVerdict:
    """The gate's answer for one call.

    ``ok`` is False when ANY path was refused. ``acknowledged`` records that an
    explicit override was honoured, and ``acknowledgment`` carries the reason
    the caller had to give -- so an override is auditable rather than a silent
    hole in the gate.
    """

    ok: bool = True
    refusals: Tuple[ReadBeforeEditRefusal, ...] = ()
    acknowledged: bool = False
    acknowledgment: str = ""
    paths_checked: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this verdict."""
        return {
            "ok": bool(self.ok),
            "refusals": [item.to_dict() for item in self.refusals],
            "acknowledged": bool(self.acknowledged),
            "acknowledgment": self.acknowledgment,
            "paths_checked": list(self.paths_checked),
            "reasons": sorted({item.reason for item in self.refusals}),
        }

    def render(self) -> str:
        """Return the model-facing refusal text, one line per refused path."""
        return "\n".join(item.render() for item in self.refusals)


def _paths_of(tool_name: str, arguments: Mapping[str, Any]) -> Tuple[str, ...]:
    """Return the repository paths one call would write.

    ``apply_patch`` carries several paths inside its patch text, so it is not
    parsed here: the gate runs on the paths the call NAMES, and a patch's
    targets are the digest check's business (``_patch_paths`` there). A gate
    that parsed patch hunks would be a second patch parser, and this repository
    has already paid for that class of bug.
    """
    data = dict(arguments or {})
    if tool_name == "apply_patch":
        return ()
    candidates = []
    for key in ("path", "destination_path", "source_path", "target", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
            break
    return tuple(candidates)


def check_read_before_edit(
    tool_name: str,
    arguments: Mapping[str, Any],
    ledger: Optional[TurnReadLedger],
    *,
    turn: Any = None,
    side_effect_class: str = "",
    config: Optional[Mapping[str, Any]] = None,
    acknowledge: str = "",
) -> ReadBeforeEditVerdict:
    """Return whether this call may write, given what the session has read.

    The whole gate, as a pure function of four inputs and one ledger, so it can
    be asserted on directly rather than only observed through a tool
    dispatch. ``TurnReadLedger`` is mutable state the caller owns; everything
    else is a value.

    Order of the rules, and why it is this order:

    1. **A read is never gated.** ``side_effect_class == "read_only"`` returns
       immediately. A gate that refused a read would make the escape
       impossible.
    2. **No ledger, no gate.** A caller that did not construct a ledger gets
       ``ok=True`` and is responsible for noticing. The alternative -- fail
       closed on a missing ledger -- would break every direct registry dispatch
       (the SDK path, a test, a custom strategy) for a gate they never asked
       for, and ``phases/DOCTRINE.md``'s "a dead guard is worse than an absent
       one" cuts the other way here: a guard that refuses everything nobody can
       use is a guard nobody turns on.
    3. **The run's own mutations count as knowledge.** A path this run just
       wrote is not unread.
    4. **``never_read`` first, then ``same_turn``.** A path that was never read
       reports that, not a same-turn violation it cannot be.
    5. **An explicit acknowledgment overrides**, and is RECORDED. The brief's
       requirement is "refused **or** requires an explicit acknowledgment"; a
       gate with no override is a gate a legitimate one-shot has to route
       around. An empty acknowledgment string is NOT an override, so a caller
       cannot acknowledge by accident.
    """
    if str(side_effect_class or "") == "read_only":
        return ReadBeforeEditVerdict(ok=True)
    if ledger is None:
        return ReadBeforeEditVerdict(ok=True)
    paths = _paths_of(str(tool_name or ""), arguments)
    if not paths:
        return ReadBeforeEditVerdict(ok=True)

    never_read_on = read_before_edit_enabled(config)
    same_turn_on = read_before_edit_same_turn_enabled(config)
    if not never_read_on and not same_turn_on:
        return ReadBeforeEditVerdict(ok=True, paths_checked=paths)

    turn_index = _as_turn(turn)
    refusals: List[ReadBeforeEditRefusal] = []
    for path in paths:
        key = TurnReadLedger.key(path)
        if ledger.was_mutated_by_run(key):
            continue
        if not ledger.was_read(key):
            if never_read_on:
                refusals.append(
                    ReadBeforeEditRefusal(
                        path=key,
                        reason="never_read",
                        detail="No observation of this file exists in this session.",
                    )
                )
            continue
        if same_turn_on and turn_index is not None:
            first = ledger.first_turn(key)
            if first is not None and first >= 0 and first == turn_index:
                refusals.append(
                    ReadBeforeEditRefusal(
                        path=key,
                        reason="same_turn",
                        detail=(
                            f"It was read on turn {turn_index}, which is the turn "
                            "this write is on."
                        ),
                    )
                )

    if not refusals:
        return ReadBeforeEditVerdict(ok=True, paths_checked=paths)
    reason = str(acknowledge or "").strip()
    if reason:
        return ReadBeforeEditVerdict(
            ok=True,
            refusals=tuple(refusals),
            acknowledged=True,
            acknowledgment=reason,
            paths_checked=paths,
        )
    return ReadBeforeEditVerdict(ok=False, refusals=tuple(refusals), paths_checked=paths)


def read_before_edit_report(
    ledger: Optional[TurnReadLedger],
    config: Optional[Mapping[str, Any]],
    *,
    refusals: Sequence[ReadBeforeEditVerdict] = (),
) -> Dict[str, Any]:
    """Return the run-level receipt: is the gate armed, and what did it refuse.

    Reported once per run rather than per call, so a receipt answers "was this
    guard doing anything at all?" -- the question a per-call refusal log cannot
    answer, because a gate that never fired and a gate that was never armed look
    identical in a log.
    """
    gated = [item for item in refusals if item.refusals]
    return {
        "armed_never_read": read_before_edit_enabled(config),
        "armed_same_turn": read_before_edit_same_turn_enabled(config),
        "ledger_bound": ledger is not None,
        "paths_tracked": len(ledger) if ledger is not None else 0,
        "calls_gated": len(gated),
        "paths_refused": sum(len(item.refusals) for item in gated),
        "acknowledged": sum(1 for item in gated if item.acknowledged),
        "reasons": sorted({r.reason for item in gated for r in item.refusals}),
        "armed_and_did_nothing": bool(
            (read_before_edit_enabled(config) or read_before_edit_same_turn_enabled(config))
            and ledger is not None
            and not gated
        ),
        "ledger": ledger.report() if ledger is not None else None,
        "note": (
            "armed_and_did_nothing distinguishes 'the guard fired zero times' from "
            "'the guard was never armed'. It is False when the gate is ARMED and no "
            "LEDGER is bound, because that is a gate that could never fire -- and "
            "reporting it as 'did nothing' would hide the missing ledger. A gate that "
            "is never armed is not a gate."
        ),
    }
