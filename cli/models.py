"""The model picker and the effort VARIANT, as one non-blocking state machine.

This module is deliberately **pure and UI-free**. It imports no Textual, reads
no settings file, opens no socket and starts no loop; every public function is
total and returns a value. That is not tidiness, it is the reason the surface
can be a *picker* rather than a wizard: a picker is a list the user looks at
while the session keeps running, and a wizard is a modal that owns the session
until it is dismissed. Anything with I/O in it eventually becomes the second
kind.

The mount point for a Textual surface is therefore one widget
(:data:`PICKER_WIDGET_ID`) wrapping a :class:`ModelPicker`, plus one binding
(:data:`VARIANT_CYCLE_KEYBIND`). Both names are re-exported from
``runtime.model_capabilities`` so a surface cannot bind a key that walks a
different vocabulary than the one the receipt reports. ``cli/tui.py`` is not
imported here and this module never imports it.

Three rules the whole file exists to enforce.

1. **A level that is not sent is reported as not sent.** The effort authority
   is ``runtime.model_capabilities`` (AGT-08) and this module does not
   re-implement it. What this module adds is the CHOICE vocabulary per family
   and a receipt that separates ``level`` (what the user picked) from
   ``honoured`` (whether a real provider parameter is on the request). A surface
   that renders one where it means the other is the "set to high that silently
   does nothing" bug, and the two are separate keys so it cannot.

2. **An unrecognised model id is refused and the current model is kept.** A
   refused selection returns a reason from a closed vocabulary and a list of
   near misses. It never clears the model, never half-applies, and never leaves
   the user with nothing: a refusal is an outcome, not a dead end.

3. **A render failure must never delete a message.** Every line this module
   produces is PLAIN text and is returned by :func:`plain_lines` /
   :func:`safe_lines`. A model id, a provider detail or a reason is DATA; it
   never crosses into a markup string, and :func:`safe_lines` returns
   ``rich.text.Text`` objects, which are structurally immune to a markup parser
   eating a ``[...]`` that came from a model name. A caller that must have
   strings uses :func:`escape_lines`.

**The anti-clutter rule is applied to group HEADERS, not to models.**
:func:`ModelPicker.sections` renders a header only for a group with three or
more entries; a group of one or two still contributes its rows, each tagged with
its group so a surface can label them however it likes. Rendering a header for
two rows spends a row of chrome on two facts, which is what the rule forbids;
dropping the two models would be worse than either.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cli.fuzzy import fuzzy_score

__all__ = [
    "EFFORT_CYCLE_KEY",
    "MAX_VISIBLE_MODELS",
    "MIN_GROUP_ROWS_FOR_HEADER",
    "MODEL_ID_REJECTIONS",
    "PICKER_ACTIONS",
    "PICKER_GROUPS",
    "PICKER_KEYS",
    "PICKER_WIDGET_ID",
    "VARIANT_CYCLE_KEYBIND",
    "EffortVariant",
    "ModelDiscovery",
    "ModelEntry",
    "ModelIdVerdict",
    "ModelPicker",
    "ModelSelection",
    "PickerRow",
    "PickerSection",
    "cmd_models",
    "cycle_effort",
    "discover_models",
    "escape_lines",
    "model_catalog",
    "model_id_verdict",
    "picker_widget_id",
    "plain_lines",
    "register_models_parser",
    "safe_lines",
    "select_model",
]

#: The widget id a Textual surface should mount the picker under. Declared here
#: because the picker is this module's and the surface is not: a surface that
#: invents its own id is a surface whose test cannot find the picker.
PICKER_WIDGET_ID = "neo-model-picker"

#: Alias kept because both spellings are in circulation across terminals.
EFFORT_CYCLE_KEY = "variant.cycle"


# ---------------------------------------------------------------------------
# The runtime seam, imported defensively
# ---------------------------------------------------------------------------
#
# `cli` must stay importable on a path with no runtime package, and the command
# registry above this module has been taken offline by a broken import before.
# So the effort authority is reached through one small resolver and every
# caller goes through the wrappers below.


def _mc() -> Any:
    """Return ``runtime.model_capabilities`` or ``None``. Never raises."""
    try:
        from runtime import model_capabilities

        return model_capabilities
    except Exception:  # pragma: no cover - a broken install, not a code path
        return None


def _variant_keybind() -> str:
    """The variant keybind, from the authority when it is reachable."""
    module = _mc()
    getter = getattr(module, "variant_keybind", None) if module else None
    if callable(getter):
        try:
            return str(getter())
        except Exception:  # pragma: no cover - defensive
            return EFFORT_CYCLE_KEY
    return EFFORT_CYCLE_KEY


#: Re-exported so a surface imports ONE module for the picker and its keybind.
VARIANT_CYCLE_KEYBIND = _variant_keybind()

#: At most this many model ROWS are visible. Group headers do not count: the
#: bound is on how many models a person can look at, and a surface that spends
#: a header row inside the bound would show fewer models the richer the list.
MAX_VISIBLE_MODELS = 8

#: A group renders a header only at or above this size (the anti-clutter rule).
MIN_GROUP_ROWS_FOR_HEADER = 3

#: Group order, and it is an order, not a set: favorites, then recent, then the
#: per-provider groups. Favorites first because they are the ones a person is
#: most likely to want, and the whole list is ranked inside each group.
PICKER_GROUPS: Tuple[str, ...] = ("favorites", "recent", "provider")

#: Closed vocabulary for a refused model id. A refusal is machine-readable
#: first, because a log reader has to be able to count them.
MODEL_ID_REJECTIONS: Tuple[str, ...] = ("empty", "unsafe", "typo", "incomplete")

#: Closed vocabulary of what a keypress did.
PICKER_ACTIONS: Tuple[str, ...] = (
    "inactive",
    "ignored",
    "query",
    "move",
    "select",
    "dismiss",
    "variant",
    "no_results",
)

#: Every key the picker understands. A single printable character is appended to
#: the query instead - that is the free-text field, and it is a KEY here so a
#: surface's binding table and the picker's own vocabulary cannot disagree.
PICKER_KEYS: Tuple[str, ...] = (
    "up",
    "down",
    "k",
    "j",
    "pageup",
    "pagedown",
    "home",
    "end",
    "enter",
    "escape",
    "backspace",
    VARIANT_CYCLE_KEYBIND,
)

#: A model id is an identifier, not a path and not a URL. Anything outside this
#: shape is refused before it is stored, which is what keeps an arbitrary
#: free-text field from becoming an arbitrary-write field.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*(?:/[A-Za-z0-9][A-Za-z0-9._:+-]*)*$")
_MAX_ID_LEN = 200


def picker_widget_id() -> str:
    """Return the widget id a surface should mount the picker under."""
    return PICKER_WIDGET_ID


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelEntry:
    """One selectable ``(provider, model)`` pair and where it came from.

    ``sources`` is a tuple of slugs, ordered by how deliberate the source is
    (``favorite`` before ``recent`` before ``settings`` before ``registry``), so
    a surface can explain a row instead of asserting that it exists. **No
    credential, endpoint or profile body is ever stored here**: the catalog is a
    list of names a picker renders, and a rendered list is the one place a
    secret is most likely to be printed by accident.
    """

    model: str
    provider: str = ""
    sources: Tuple[str, ...] = ()
    hint: str = ""

    @property
    def id(self) -> str:
        """The ``provider/model`` id, or the bare model when unprefixed."""
        return f"{self.provider}/{self.model}" if self.provider else self.model

    @property
    def family(self) -> str:
        """The effort family for this entry, or ``""`` when none applies."""
        module = _mc()
        resolver = getattr(module, "effort_family_for", None) if module else None
        if not callable(resolver):
            return ""
        try:
            return str(resolver(self.model, self.provider))
        except Exception:  # pragma: no cover - defensive
            return ""

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe row. Carries no secret by construction - see the class."""
        return {
            "id": self.id,
            "model": self.model,
            "provider": self.provider,
            "sources": list(self.sources),
            "hint": self.hint,
            "family": self.family,
        }


def _entry_key(provider: Any, model: Any) -> Tuple[str, str]:
    return (str(provider or "").strip().lower(), str(model or "").strip())


def _add(
    out: Dict[Tuple[str, str], ModelEntry],
    provider: Any,
    model: Any,
    source: str,
    hint: str = "",
) -> None:
    """Add one entry, merging sources onto an existing row rather than duping."""
    name = str(model or "").strip()
    if not name:
        return
    prov = str(provider or "").strip()
    key = _entry_key(prov, name)
    existing = out.get(key)
    if existing is None:
        out[key] = ModelEntry(model=name, provider=prov, sources=(source,), hint=hint)
        return
    if source not in existing.sources:
        out[key] = ModelEntry(
            model=existing.model,
            provider=existing.provider,
            sources=(*existing.sources, source),
            hint=existing.hint or hint,
        )


def _split_id(value: Any, default_provider: Any = "") -> Tuple[str, str]:
    """Split ``provider/model`` into its parts, tolerating a bare name."""
    text = str(value or "").strip()
    if not text:
        return "", ""
    if "/" in text:
        head, tail = text.split("/", 1)
        if head and tail:
            return head.strip(), tail.strip()
    return str(default_provider or "").strip(), text


def model_catalog(
    settings: Optional[Mapping[str, Any]] = None,
    *,
    favorites: Sequence[str] = (),
    recent: Sequence[str] = (),
    extra: Sequence[Any] = (),
    include_registry: bool = True,
) -> List[ModelEntry]:
    """Return the selectable models, in group order, deduplicated.

    Sources, in the order they are added, because the order IS the grouping
    priority: ``favorites`` first, then ``recent``, then the settings-declared
    model, the router's tier table, named provider profiles, and finally the
    capability registry's known rows. Every source is optional and every one of
    them can be absent without the catalog becoming empty-but-successful: with
    no sources at all the catalog is empty and the picker says so.

    ``settings`` is read by key meaning, and only these keys: ``model``,
    ``provider``, ``model_tiers``, ``provider_profiles``, ``favorites``,
    ``recent_models``. ``api_key``/``api_base``/``base_url`` are never read -
    a catalog is a list of names and this is the module that would print it.
    """
    out: Dict[Tuple[str, str], ModelEntry] = {}
    cfg: Mapping[str, Any] = settings if isinstance(settings, Mapping) else {}

    for value in favorites or ():
        provider, model = _split_id(value, cfg.get("provider"))
        _add(out, provider, model, "favorite")

    for value in recent or ():
        provider, model = _split_id(value, cfg.get("provider"))
        _add(out, provider, model, "recent")

    for value in cfg.get("favorites") or ():
        provider, model = _split_id(value, cfg.get("provider"))
        _add(out, provider, model, "favorite")

    for value in cfg.get("recent_models") or ():
        provider, model = _split_id(value, cfg.get("provider"))
        _add(out, provider, model, "recent")

    provider, model = _split_id(cfg.get("model"), cfg.get("provider"))
    _add(out, provider, model, "settings", "the configured model")

    tiers = cfg.get("model_tiers")
    if isinstance(tiers, Mapping):
        for hint in sorted(tiers):
            tier = tiers.get(hint)
            if not isinstance(tier, Mapping):
                continue
            _add(out, tier.get("provider"), tier.get("model"), "tier", str(hint))

    profiles = cfg.get("provider_profiles")
    if isinstance(profiles, Mapping):
        for name in sorted(profiles):
            profile = profiles.get(name)
            if not isinstance(profile, Mapping):
                continue
            _add(
                out,
                profile.get("provider"),
                profile.get("model"),
                "profile",
                str(name),
            )

    if include_registry:
        for value in extra or ():
            provider, model = _split_id(value)
            _add(out, provider, model, "declared")
        try:
            from runtime.config import DEFAULT_MODEL_TIERS

            for hint in sorted(DEFAULT_MODEL_TIERS):
                tier = DEFAULT_MODEL_TIERS[hint] or {}
                _add(out, tier.get("provider"), tier.get("model"), "tier", str(hint))
        except Exception:  # pragma: no cover - a path with no runtime package
            pass
        module = _mc()
        known = getattr(module, "known_capabilities", None) if module else None
        if callable(known):
            try:
                for row in known():
                    _add(
                        out,
                        getattr(row, "provider", ""),
                        getattr(row, "model", ""),
                        "registry",
                    )
            except Exception:  # pragma: no cover - defensive
                pass
    elif extra:
        for value in extra:
            provider, model = _split_id(value)
            _add(out, provider, model, "declared")

    return [out[key] for key in out]


# ---------------------------------------------------------------------------
# The effort variant
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffortVariant:
    """One effort choice and the honest answer for the current model.

    ``level`` is what the user picked. ``honoured`` is whether a real provider
    parameter is on the request. They are separate fields because a receipt
    that conflates them is the defect this round closes, and ``parameter`` /
    ``value`` are present even when ``honoured`` is False so a surface can name
    WHICH parameter was declined.
    """

    level: str
    vocabulary: Tuple[str, ...] = ()
    honoured: bool = False
    parameter: str = ""
    value: Any = None
    status: str = ""
    family: str = ""
    detail: str = ""
    effective_effort: str = "auto"
    keybind: str = VARIANT_CYCLE_KEYBIND

    @property
    def in_vocabulary(self) -> bool:
        """Whether ``level`` is one of this model's own choices."""
        return bool(self.level) and self.level in self.vocabulary

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe receipt. ``honoured`` is the field that may not lie."""
        return {
            "level": self.level,
            "effective_effort": self.effective_effort,
            "vocabulary": list(self.vocabulary),
            "in_vocabulary": self.in_vocabulary,
            "honoured": self.honoured,
            "parameter": self.parameter,
            "value": self.value,
            "status": self.status,
            "family": self.family,
            "detail": self.detail,
            "keybind": self.keybind,
        }

    def lines(self) -> List[str]:
        """Render the variant as PLAIN, markup-free lines.

        The first line is authored here and can never open with ``[``, so a
        renderer cannot eat the label. Every other line carries DATA and is the
        caller's to escape - see :func:`escape_lines`.
        """
        out = [f"effort: {self.level or 'auto'}"]
        out.append(f"  choices: {'/'.join(self.vocabulary) or 'none declared'}")
        if self.honoured:
            out.append(
                f"  sends: {self.parameter} = {self.value!r} ({self.family}); "
                f"run config records effort={self.effective_effort}"
            )
        else:
            out.append(f"  nothing sent: {self.status or 'unsupported'}")
            if self.parameter:
                out.append(f"  this provider sends {self.parameter} for other levels")
            out.append(f"  run config records effort={self.effective_effort}")
        if self.detail:
            out.append(f"  {self.detail}")
        out.append(f"  cycle with: {self.keybind}")
        return out


def _variant_from(receipt: Mapping[str, Any]) -> EffortVariant:
    return EffortVariant(
        level=str(receipt.get("level") or ""),
        vocabulary=tuple(str(item) for item in receipt.get("vocabulary") or ()),
        honoured=bool(receipt.get("honoured")),
        parameter=str(receipt.get("parameter") or ""),
        value=receipt.get("value"),
        status=str(receipt.get("status") or ""),
        family=str(receipt.get("family") or ""),
        detail=str(receipt.get("detail") or ""),
        effective_effort=str(receipt.get("effective_effort") or "auto"),
        keybind=str(receipt.get("keybind") or VARIANT_CYCLE_KEYBIND),
    )


def effort_variant(
    level: Any,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    *,
    parameter: Any = None,
) -> EffortVariant:
    """Resolve one effort choice against a model. Never raises."""
    module = _mc()
    resolver = getattr(module, "picker_variant", None) if module else None
    if callable(resolver):
        try:
            return _variant_from(
                resolver(level, model, provider=provider, parameter=parameter)
            )
        except Exception as exc:  # pragma: no cover - defensive
            return EffortVariant(
                level=str(level or "").strip().lower(),
                status="unreported",
                detail=f"the effort authority is unavailable: {exc}",
            )
    # No runtime package: report the ladder as unavailable rather than
    # pretending a level was sent.
    return EffortVariant(
        level=str(level or "").strip().lower(),
        status="unreported",
        detail="the effort authority is unavailable: runtime.model_capabilities",
    )


def cycle_effort(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    current: Any = None,
    *,
    parameter: Any = None,
) -> EffortVariant:
    """Move to the next rung of THIS model's vocabulary and return its receipt.

    Wraps at the end of the vocabulary. A model with no declared knob has a
    vocabulary of one, so this is a stable no-op for it rather than a surprise.
    """
    module = _mc()
    cycler = getattr(module, "next_picker_level", None) if module else None
    level = current
    if callable(cycler):
        try:
            level = cycler(model, provider, current, parameter=parameter)
        except Exception:  # pragma: no cover - defensive
            level = current
    return effort_variant(level, model, provider, parameter=parameter)


def carry_effort(
    previous: Any,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    *,
    parameter: Any = None,
) -> Dict[str, Any]:
    """Carry an effort level across a MODEL change. Never raises.

    Delegates to the runtime authority so the picker and the router cannot
    disagree about whether a carried level was honoured.
    """
    module = _mc()
    carrier = getattr(module, "carry_picker_effort", None) if module else None
    if callable(carrier):
        try:
            return dict(
                carrier(previous, model, provider=provider, parameter=parameter)
            )
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "previous": str(previous or ""),
                "level": "auto",
                "carried": "dropped",
                "changed": True,
                "vocabulary": [],
                "sent": False,
                "parameter": "",
                "value": None,
                "status": "unreported",
                "family": "",
                "detail": f"the effort authority is unavailable: {exc}",
                "plan": {},
            }
    return {
        "previous": str(previous or ""),
        "level": "auto",
        "carried": "dropped",
        "changed": True,
        "vocabulary": [],
        "sent": False,
        "parameter": "",
        "value": None,
        "status": "unreported",
        "family": "",
        "detail": "the effort authority is unavailable: runtime.model_capabilities",
        "plan": {},
    }


# ---------------------------------------------------------------------------
# Model-id validation
# ---------------------------------------------------------------------------


def _edit_distance_at_most(
    left: str, right: str, limit: int, left_counts: Optional[Counter] = None
) -> int:
    """Levenshtein distance, abandoned the moment it provably exceeds ``limit``.

    Bounded because a picker must stay instant. Two cheap screens run before
    the DP, both of which can only REJECT a candidate (never accept one), so
    they cost nothing in correctness:

    * a length screen - strings more than ``limit`` apart in length cannot be
      within ``limit`` edits;
    * a character-multiset screen - if two strings are within ``limit`` edits
      then no character's count can differ by more than ``limit``. The counters
      are built in C, so this screen is roughly an order of magnitude cheaper
      than the row of DP cells it replaces, and it is the screen that actually
      bites on a catalog of same-length model names (the length screen cannot
      fire there at all).

    ``left_counts`` is the caller's pre-built counter for ``left``. Building it
    once per query instead of once per candidate is worth a measured 5x on the
    whole check, and it is the difference between a sub-millisecond submit and
    a visible stall on a large catalog.
    """
    if left == right:
        return 0
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    if left_counts is None:
        left_counts = Counter(left)
    shared = (left_counts & Counter(right)).values()
    if sum(shared) < len(left) - limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for i, lch in enumerate(left, start=1):
        current = [i]
        for j, rch in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (lch != rch),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _typo_limit(name: str) -> int:
    """How many edits still counts as a typo for a name of this length."""
    return 2 if len(name) >= 6 else 1


@dataclass(frozen=True)
class ModelIdVerdict:
    """The answer to "is this a model id this session should accept?".

    ``accepted`` False always means the CURRENT MODEL IS KEPT. The verdict
    carries the model that is still in force so a surface can print the
    rejection and the surviving model in the same breath, which is the whole
    point of refusing rather than clearing.
    """

    accepted: bool
    reason: str = ""
    message: str = ""
    suggestions: Tuple[str, ...] = ()
    model: str = ""
    provider: str = ""
    current_model: str = ""
    kept_current: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe row for a transcript, a journal, or a test."""
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "message": self.message,
            "suggestions": list(self.suggestions),
            "model": self.model,
            "provider": self.provider,
            "current_model": self.current_model,
            "kept_current": self.kept_current,
        }


def model_id_verdict(
    text: Any,
    catalog: Sequence[ModelEntry] = (),
    *,
    current_model: str = "",
    current_provider: str = "",
) -> ModelIdVerdict:
    """Decide whether a free-text model id may be set, and say why when not.

    The tension this resolves: a bring-your-own router means a session must be
    able to name a model nobody has heard of, so a plain unknown id is
    ACCEPTED. What is refused is the three cases where accepting produces a
    failure three hours later instead of now - an id that is not an identifier
    at all, an id with no model after its provider prefix, and an id that is a
    near miss for a model this install already knows (a typo is the one case
    where the product has better information than the person typing, and the
    near-miss list is that information).

    Never raises; an unusable catalog is an empty catalog, which simply means
    no typo detection.
    """
    raw = "" if text is None else str(text)
    current = str(current_model or "").strip()
    kept = bool(current)
    known_ids = [entry.id for entry in catalog if entry.id]

    if not raw.strip():
        return ModelIdVerdict(
            accepted=False,
            reason="empty",
            message=(
                f"no model id was given; the model is unchanged at {current or 'unset'}"
            ),
            current_model=current,
            kept_current=kept,
        )

    candidate = raw.strip()
    if len(candidate) > _MAX_ID_LEN:
        return ModelIdVerdict(
            accepted=False,
            reason="unsafe",
            message=(
                f"a model id longer than {_MAX_ID_LEN} characters is not a model id; "
                f"the model is unchanged at {current or 'unset'}"
            ),
            current_model=current,
            kept_current=kept,
        )
    if candidate.endswith("/") or candidate.startswith("/"):
        return ModelIdVerdict(
            accepted=False,
            reason="incomplete",
            message=(
                f"{candidate!r} names a provider but no model; the model is "
                f"unchanged at {current or 'unset'}"
            ),
            current_model=current,
            kept_current=kept,
        )
    if not _ID_RE.match(candidate):
        return ModelIdVerdict(
            accepted=False,
            reason="unsafe",
            message=(
                f"{candidate!r} is not a model id: it must be letters, digits, "
                "'.', '_', ':', '+', '-' and at most one 'provider/' prefix, with "
                f"no spaces, control characters or path segments; the model is "
                f"unchanged at {current or 'unset'}"
            ),
            current_model=current,
            kept_current=kept,
        )

    provider, model = _split_id(candidate)
    for entry in catalog:
        if entry.model.lower() == model.lower() and (
            not provider or entry.provider.lower() == provider.lower()
        ):
            return ModelIdVerdict(
                accepted=True,
                model=entry.model,
                provider=entry.provider or provider,
                message=f"using {entry.id}",
                current_model=current,
                kept_current=current == entry.model,
            )

    # Both counters are built ONCE for the query, not once per candidate.
    full_counts = Counter(candidate.lower())
    bare_counts = Counter(model.lower())
    near: List[str] = []
    for known in known_ids:
        limit_here = _typo_limit(known.rsplit("/", 1)[-1])
        if (
            _edit_distance_at_most(
                candidate.lower(), known.lower(), limit_here, full_counts
            )
            <= limit_here
        ):
            near.append(known)
    if not near:
        # A bare name compared against a BARE name, and the suggestion is the
        # catalog's OWN id: offering `gpt-4o-mini` when the picker row says
        # `openai/gpt-4o-mini` is a suggestion the person cannot select.
        by_bare: Dict[str, str] = {}
        for entry in catalog:
            if entry.model:
                by_bare.setdefault(entry.model.lower(), entry.id)
        for bare, identifier in by_bare.items():
            limit_here = _typo_limit(bare)
            if (
                _edit_distance_at_most(model.lower(), bare, limit_here, bare_counts)
                <= limit_here
            ):
                near.append(identifier)
    if near:
        uniq = sorted(dict.fromkeys(near))
        return ModelIdVerdict(
            accepted=False,
            reason="typo",
            message=(
                f"{candidate!r} is not a model this install knows, and it is one "
                f"edit away from {', '.join(uniq)}; the model is unchanged at "
                f"{current or 'unset'}"
            ),
            suggestions=tuple(uniq),
            current_model=current,
            kept_current=kept,
        )

    return ModelIdVerdict(
        accepted=True,
        model=model,
        provider=provider or current_provider,
        message=(
            f"using {provider + '/' if provider else ''}{model} "
            "(not in this install's catalog; sent as given)"
        ),
        current_model=current,
        kept_current=kept,
    )


@dataclass(frozen=True)
class ModelSelection:
    """The outcome of choosing a model, with the effort variant CHAINED onto it.

    ``variant`` is not a second step and not a dialog: it is the receipt for the
    effort level now in force for the model just chosen, produced by the same
    call. That is the whole "chained immediately after model selection" rule -
    a separate screen would be a second thing to dismiss before the first
    change takes effect, and a change that is not visible until a second
    dismissal is a change the user did not make.
    """

    ok: bool
    model: str = ""
    provider: str = ""
    previous_model: str = ""
    reason: str = ""
    message: str = ""
    suggestions: Tuple[str, ...] = ()
    kept_current: bool = False
    variant: Optional[EffortVariant] = None
    carry: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe row. ``variant`` rides as a dict, never as an object."""
        return {
            "ok": self.ok,
            "model": self.model,
            "provider": self.provider,
            "previous_model": self.previous_model,
            "reason": self.reason,
            "message": self.message,
            "suggestions": list(self.suggestions),
            "kept_current": self.kept_current,
            "variant": self.variant.to_dict() if self.variant else None,
            "carry": dict(self.carry),
        }

    def lines(self) -> List[str]:
        """Render the selection as PLAIN, markup-free lines."""
        if not self.ok:
            head = f"model unchanged at {self.model or 'unset'}"
            out = [head, f"  {self.message}"]
            if self.suggestions:
                out.append(f"  did you mean: {', '.join(self.suggestions)}")
            return out
        out = [f"model: {self.model}"]
        if self.provider:
            out.append(f"  provider: {self.provider}")
        if self.carry:
            carried = str(self.carry.get("carried") or "")
            if carried == "mapped":
                out.append(
                    f"  effort carried as {self.carry.get('level')!r} "
                    f"({self.carry.get('parameter')}="
                    f"{self.carry.get('value')!r})"
                )
            elif carried == "dropped":
                out.append(
                    f"  effort {self.carry.get('previous')!r} is not available on "
                    f"this model: {self.carry.get('level')!r}"
                )
        if self.variant is not None:
            out.extend(self.variant.lines())
        return out


def select_model(
    typed: Any,
    catalog: Sequence[ModelEntry] = (),
    *,
    current_model: str = "",
    current_provider: str = "",
    current_effort: Any = None,
    parameter: Any = None,
) -> ModelSelection:
    """Set a model from a free-text id, refusing an unusable one in place.

    On refusal the returned selection carries the CURRENT model and
    ``kept_current=True``: the session keeps a working model, and the reason is
    a row in a closed vocabulary rather than a swallowed exception. On
    acceptance the effort level in force is carried across the model change
    through the one authority (:func:`carry_effort`) and the resulting variant
    is chained onto the same return value.
    """
    current = str(current_model or "").strip()
    verdict = model_id_verdict(
        typed,
        catalog,
        current_model=current,
        current_provider=current_provider,
    )
    if not verdict.accepted:
        return ModelSelection(
            ok=False,
            model=current,
            provider=current_provider,
            previous_model=current,
            reason=verdict.reason,
            message=verdict.message,
            suggestions=verdict.suggestions,
            kept_current=bool(current),
        )
    carry = carry_effort(
        current_effort if current_effort is not None else "auto",
        verdict.model,
        verdict.provider,
        parameter=parameter,
    )
    return ModelSelection(
        ok=True,
        model=verdict.model,
        provider=verdict.provider,
        previous_model=current,
        message=verdict.message,
        kept_current=verdict.model == current,
        variant=effort_variant(
            carry.get("level"), verdict.model, verdict.provider, parameter=parameter
        ),
        carry=carry,
    )


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PickerRow:
    """One visible model row. ``index`` is its position in the picker's list."""

    index: int
    group: str
    entry: ModelEntry
    selected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe row for a surface that renders the model itself."""
        return {
            "index": self.index,
            "group": self.group,
            "selected": self.selected,
            **self.entry.to_dict(),
        }


@dataclass(frozen=True)
class PickerSection:
    """A group of rows, with the header the anti-clutter rule decided on.

    ``header`` is ``""`` for a group of one or two rows. The rows are still
    there; only the chrome is withheld. ``provider`` names which provider block
    a section is, so the per-provider sections inside the ``provider`` group
    are distinguishable - they are separate visual groups and the rule applies
    to each of them.
    """

    group: str
    title: str
    rows: Tuple[PickerRow, ...]
    header: str = ""
    provider: str = ""

    @property
    def size(self) -> int:
        return len(self.rows)

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe section."""
        return {
            "group": self.group,
            "title": self.title,
            "header": self.header,
            "provider": self.provider,
            "size": self.size,
            "rows": [row.to_dict() for row in self.rows],
        }


_GROUP_TITLES = {"favorites": "Favorites", "recent": "Recent"}


def _group_for(entry: ModelEntry) -> str:
    if "favorite" in entry.sources:
        return "favorites"
    if "recent" in entry.sources:
        return "recent"
    return "provider"


def _provider_order(rows: Sequence[PickerRow]) -> List[str]:
    """The providers present, in the order they already appear in the rows.

    Reading the order off the rows rather than re-sorting keeps the section
    order identical to the row order, so a surface that renders sections and
    one that renders rows cannot disagree about what comes first.
    """
    out: List[str] = []
    for row in rows:
        if row.entry.provider not in out:
            out.append(row.entry.provider)
    return out


class ModelPicker:
    """A keyboard-driven model list with the effort variant chained onto it.

    The whole picker is state plus pure transitions. There is no ``await``, no
    timer, no I/O and no callback into a run loop, which is what makes it safe
    to keep OPEN while a session continues: nothing here can block the session
    because nothing here can block at all.

    Every key returns a slug from :data:`PICKER_ACTIONS`, so a surface can
    assert on behaviour instead of on a rendered string, and an inactive picker
    answers ``"inactive"`` to everything - a stray keypress must never drive a
    picker nobody is looking at.
    """

    def __init__(
        self,
        catalog: Sequence[ModelEntry] = (),
        *,
        current_model: str = "",
        current_provider: str = "",
        current_effort: Any = "auto",
        max_visible: int = MAX_VISIBLE_MODELS,
        parameter: Any = None,
        active: bool = True,
    ) -> None:
        self.catalog: List[ModelEntry] = list(catalog)
        self.current_model = str(current_model or "")
        self.current_provider = str(current_provider or "")
        self.current_effort = str(current_effort or "auto")
        self.parameter = parameter
        self.max_visible = max(1, int(max_visible or MAX_VISIBLE_MODELS))
        self.query = ""
        self.cursor = 0
        self.active = bool(active)
        # The matched set depends on the QUERY and the catalog, and nothing
        # else. A frame asks for it several times (visible, hidden_count,
        # sections, lines), so it is computed once per query rather than once
        # per question. Measured on this host: one pass over a 2,000-row
        # catalog is 27 ms, so the un-cached frame was paying that three times.
        self._match_cache: Optional[
            Tuple[Tuple[str, int, int], Tuple[Tuple[str, ModelEntry], ...]]
        ] = None

    # -- state -------------------------------------------------------------

    def open(self) -> "ModelPicker":
        """Make the picker respond to keys. Returns self for chaining."""
        self.active = True
        return self

    def close(self) -> "ModelPicker":
        """Make the picker inert. The session is untouched by this."""
        self.active = False
        return self

    def set_query(self, text: Any) -> str:
        """Replace the query. Never raises; a hostile value becomes a no-op."""
        try:
            self.query = "" if text is None else str(text)
        except Exception:  # pragma: no cover - a __str__ that raises
            self.query = ""
        self.cursor = 0
        self._match_cache = None
        return "query"

    def push_char(self, char: str) -> str:
        """Append one character to the query (the free-text field)."""
        self.set_query(self.query + str(char or ""))
        return "query"

    def backspace(self) -> str:
        """Remove the last character of the query."""
        self.set_query(self.query[:-1])
        return "query"

    # -- filtering and ordering -------------------------------------------

    def _ordered(self) -> List[Tuple[str, ModelEntry]]:
        """Favorites, then recent, then provider groups; stable within a group.

        The query ranks WITHIN a group and never ACROSS groups. A global
        ranking would let a fuzzy match on one provider's model push a
        favourite off the top of the list, which is the opposite of what a
        favourites group is for - so grouping is decided first and the query is
        applied inside it.
        """
        buckets: Dict[str, List[ModelEntry]] = {name: [] for name in PICKER_GROUPS}
        for entry in self.catalog:
            buckets.setdefault(_group_for(entry), []).append(entry)
        out: List[Tuple[str, ModelEntry]] = []
        for group in PICKER_GROUPS:
            entries = buckets.get(group) or []
            if not entries:
                continue
            if group == "provider":
                by_provider: Dict[str, List[ModelEntry]] = {}
                for entry in entries:
                    by_provider.setdefault(entry.provider, []).append(entry)
                for provider in sorted(
                    by_provider, key=lambda name: (name.lower(), name)
                ):
                    for entry in self._rank(by_provider[provider]):
                        out.append((group, entry))
                continue
            for entry in self._rank(entries):
                out.append((group, entry))
        return out

    def _rank(self, entries: Sequence[ModelEntry]) -> List[ModelEntry]:
        """Fuzzy-rank one group's entries against the query, stably.

        An empty query keeps the declared order, which is what makes the
        favourites group meaningful before a character is typed. A query that
        matches nothing in this group drops the group entirely rather than
        showing it unfiltered.
        """
        scored: List[Tuple[int, int, ModelEntry]] = []
        for index, entry in enumerate(entries):
            score = fuzzy_score(self.query, entry.id)
            if score is None:
                score = fuzzy_score(self.query, entry.model)
            if score is None and str(self.query or "").strip():
                continue
            scored.append((-int(score or 0), index, entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [entry for _score, _index, entry in scored]

    # -- rows and sections -------------------------------------------------

    def _matched(self) -> Tuple[Tuple[str, ModelEntry], ...]:
        """The ordered, filtered rows for the current query. Cached per query.

        The cache key carries the query, the catalog's length and the catalog
        object's identity, so a caller that mutates ``picker.catalog`` in place
        cannot be served a stale list.
        """
        key = (self.query, len(self.catalog), id(self.catalog))
        if self._match_cache is not None and self._match_cache[0] == key:
            return self._match_cache[1]
        matched = tuple(self._ordered())
        self._match_cache = (key, matched)
        return matched

    def matches(self) -> List[PickerRow]:
        """Every matching row, in order, with the cursor marked. Unbounded."""
        ordered = self._matched()
        cursor = max(0, min(self.cursor, max(0, len(ordered) - 1)))
        return [
            PickerRow(
                index=index,
                group=group,
                entry=entry,
                selected=index == cursor,
            )
            for index, (group, entry) in enumerate(ordered)
        ]

    def visible(self) -> List[PickerRow]:
        """At most :data:`MAX_VISIBLE_MODELS` rows, cursor kept in view."""
        rows = self.matches()
        if len(rows) <= self.max_visible:
            return rows
        start = max(
            0, min(self.cursor - self.max_visible // 2, len(rows) - self.max_visible)
        )
        return rows[start : start + self.max_visible]

    def hidden_count(self) -> int:
        """How many matching rows the bound is hiding.

        Reported so a truncated list is never read as a complete one - a
        picker that silently shows 8 of 40 is worse than one that says so.
        """
        return max(0, len(self.matches()) - len(self.visible()))

    def sections(self) -> List[PickerSection]:
        """Group the visible rows, applying the anti-clutter rule to headers.

        The ``provider`` group is split into ONE SECTION PER PROVIDER, because
        that is the group a person actually sees: a merged "all providers"
        block would hide which model belongs to whom AND would let a header
        spend a row on two rows in one provider while another provider shows
        twenty. Each provider block is judged on its own size.

        A group of one or two rows renders NO header; its rows are still
        present, tagged with their group. A header is chrome, and spending a
        row of chrome on two facts is what the rule forbids.
        """
        sections: List[PickerSection] = []
        for group in PICKER_GROUPS:
            rows = [row for row in self.visible() if row.group == group]
            if not rows:
                continue
            if group == "provider":
                for provider in _provider_order(rows):
                    block = [row for row in rows if row.entry.provider == provider]
                    title = provider or "unprefixed"
                    sections.append(
                        PickerSection(
                            group=group,
                            title=title,
                            rows=tuple(block),
                            header=(
                                title if len(block) >= MIN_GROUP_ROWS_FOR_HEADER else ""
                            ),
                            provider=provider,
                        )
                    )
                continue
            title = _GROUP_TITLES.get(group, group)
            sections.append(
                PickerSection(
                    group=group,
                    title=title,
                    rows=tuple(rows),
                    header=title if len(rows) >= MIN_GROUP_ROWS_FOR_HEADER else "",
                )
            )
        return sections

    def receipt(self) -> Dict[str, Any]:
        """A machine-readable view of the whole picker, for a surface or a test."""
        sections = self.sections()
        return {
            "active": self.active,
            "widget_id": PICKER_WIDGET_ID,
            "query": self.query,
            "cursor": self.cursor,
            "model": self.current_model,
            "provider": self.current_provider,
            "effort": self.current_effort,
            "config_effort": self.config_effort(),
            "max_visible": self.max_visible,
            "visible": [row.to_dict() for row in self.visible()],
            "sections": [section.to_dict() for section in sections],
            "hidden": self.hidden_count(),
            "variant": self.variant().to_dict(),
            "keybind": VARIANT_CYCLE_KEYBIND,
        }

    # -- transitions -------------------------------------------------------

    def selected_entry(self) -> Optional[ModelEntry]:
        """The entry under the cursor, or ``None`` when nothing matches."""
        rows = self.matches()
        return rows[self.cursor].entry if rows else None

    def move(self, delta: int) -> str:
        """Move the cursor, wrapping. A no-op on an empty list, never an error."""
        rows = self.matches()
        if not rows:
            self.cursor = 0
            return "no_results"
        try:
            step = int(delta)
        except (TypeError, ValueError):
            step = 0
        self.cursor = (self.cursor + step) % len(rows)
        return "move"

    def move_to(self, index: int) -> str:
        """Put the cursor on a specific row, clamped into range."""
        rows = self.matches()
        if not rows:
            self.cursor = 0
            return "no_results"
        try:
            self.cursor = max(0, min(int(index), len(rows) - 1))
        except (TypeError, ValueError):
            return "ignored"
        return "move"

    def cycle(self) -> EffortVariant:
        """``variant.cycle``: move to the next rung for the CURRENT model.

        Bound to one key and defined on the model's own vocabulary, so the key
        cannot cycle rungs this model cannot honour. Returns the honest receipt
        for the rung now in force.
        """
        variant = cycle_effort(
            self.current_model,
            self.current_provider,
            self.current_effort,
            parameter=self.parameter,
        )
        self.current_effort = variant.level or self.current_effort
        return variant

    def variant(self) -> EffortVariant:
        """The receipt for the effort level in force right now."""
        return effort_variant(
            self.current_effort,
            self.current_model,
            self.current_provider,
            parameter=self.parameter,
        )

    def config_effort(self) -> str:
        """The effort value a run's ``Task.config`` should carry.

        This is the WIRE rung, never the choice the user picked. The difference
        is load-bearing: writing ``minimal`` into a run config would make
        ``resolve_effort`` report ``invalid:minimal`` - a typo the user never
        typed, produced by this product's own picker - and writing ``xhigh``
        would claim a level the provider cannot honour. An unhonoured choice
        therefore records ``auto``, which is the truth: nothing is sent.
        """
        return self.variant().effective_effort or "auto"

    def choose(self) -> ModelSelection:
        """Set the model under the cursor, immediately, and chain the variant.

        A row chosen here is IN FORCE when this returns: there is no confirm
        step, because a selection the user has to confirm twice is a wizard.
        Choosing with nothing under the cursor is a refusal that keeps the
        current model, with ``reason="empty"`` - the same closed vocabulary the
        free-text path uses.
        """
        entry = self.selected_entry()
        if entry is None:
            return ModelSelection(
                ok=False,
                model=self.current_model,
                provider=self.current_provider,
                previous_model=self.current_model,
                reason="empty",
                message=(
                    "no model matched; the model is unchanged at "
                    f"{self.current_model or 'unset'}"
                ),
                kept_current=bool(self.current_model),
                variant=self.variant(),
            )
        carry = carry_effort(
            self.current_effort,
            entry.model,
            entry.provider,
            parameter=self.parameter,
        )
        previous_model = self.current_model
        self.current_model = entry.model
        self.current_provider = entry.provider
        self.current_effort = str(carry.get("level") or self.current_effort)
        return ModelSelection(
            ok=True,
            model=entry.model,
            provider=entry.provider,
            previous_model=previous_model,
            message=f"using {entry.id}",
            kept_current=previous_model == entry.model,
            variant=self.variant(),
            carry=carry,
        )

    def submit_text(self, text: Any = None) -> ModelSelection:
        """The free-text field: set the model from what was typed.

        A refusal keeps the current model AND keeps the picker open with the
        text still in it, because the person needs to see what they typed to
        fix it. Never clears the model, on any path.
        """
        if text is None:
            text = self.query
        else:
            # Keep what was typed: a refusal has to leave the person looking at
            # the text they need to fix, not at an empty field.
            self.set_query(text)
        selection = select_model(
            text,
            self.catalog,
            current_model=self.current_model,
            current_provider=self.current_provider,
            current_effort=self.current_effort,
            parameter=self.parameter,
        )
        previous_model = self.current_model
        if selection.ok:
            self.current_model = selection.model
            self.current_provider = selection.provider
            self.current_effort = str(
                (selection.carry or {}).get("level") or self.current_effort
            )
        selection = ModelSelection(
            ok=selection.ok,
            model=self.current_model,
            provider=self.current_provider,
            previous_model=previous_model or selection.previous_model,
            reason=selection.reason,
            message=selection.message,
            suggestions=selection.suggestions,
            kept_current=selection.kept_current or previous_model == self.current_model,
            variant=self.variant(),
            carry=selection.carry,
        )
        return selection

    def key(self, name: str) -> str:
        """Dispatch one key. Returns a slug from :data:`PICKER_ACTIONS`.

        An INACTIVE picker answers ``"inactive"`` to everything, including
        printable characters: a keystroke must never reach a picker the user
        closed. Any other unknown key is ``"ignored"`` rather than an
        exception, because a surface with an extra binding must not crash the
        session it is decorating.
        """
        if not self.active:
            return "inactive"
        raw = "" if name is None else str(name)
        key = raw.strip().lower()
        if key == VARIANT_CYCLE_KEYBIND:
            self.cycle()
            return "variant"
        if key in PICKER_KEYS:
            if key in ("up", "k"):
                return self.move(-1)
            if key in ("down", "j"):
                return self.move(1)
            if key == "pageup":
                return self.move(-self.max_visible)
            if key == "pagedown":
                return self.move(self.max_visible)
            if key == "home":
                return self.move_to(0)
            if key == "end":
                return self.move_to(len(self.matches()) - 1)
            if key == "enter":
                return "select" if self.selected_entry() is not None else "no_results"
            if key == "escape":
                self.close()
                return "dismiss"
            if key == "backspace":
                return self.backspace()
            return "ignored"
        if len(raw) == 1 and raw.isprintable() and not raw.isspace():
            return self.push_char(raw)
        return "ignored"

    # -- rendering ---------------------------------------------------------

    def lines(self) -> List[str]:
        """Render the picker as PLAIN, markup-free lines.

        The first line is authored here and can never open with ``[``. Every
        line after it may carry a model id, a provider or a reason, all of
        which are DATA - the caller escapes them (see :func:`escape_lines`).
        """
        out = ["model picker"]
        sections = self.sections()
        if not sections:
            out.append(
                f"  no models match {self.query!r}" if self.query else "  no models"
            )
            out.append("  type a model id to use one this install does not know")
            return out
        for section in sections:
            if section.header:
                out.append(f"  {section.header}")
            for row in section.rows:
                marker = ">" if row.selected else " "
                hint = f"  ({row.entry.hint})" if row.entry.hint else ""
                out.append(f" {marker} {row.entry.id}{hint}")
        hidden = self.hidden_count()
        if hidden:
            out.append(f"  +{hidden} more not shown; keep typing to narrow")
        variant = self.variant()
        out.append(
            f"  effort: {variant.level or 'auto'} ({VARIANT_CYCLE_KEYBIND} to cycle)"
        )
        return out


# ---------------------------------------------------------------------------
# Discovery: `neo models`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelDiscovery:
    """What discovery found, and how it found it.

    ``refreshed`` is False whenever a live provider was not actually asked, and
    ``reason`` says which. A discovery command that cannot reach a provider must
    say so rather than printing a local table as though it were the provider's
    catalogue.
    """

    entries: Tuple[ModelEntry, ...]
    provider: str = ""
    sources: Tuple[str, ...] = ()
    refreshed: bool = False
    refresh_attempted: bool = False
    reason: str = ""

    def ids(self) -> List[str]:
        """The ``provider/model`` ids, in discovery order."""
        return [entry.id for entry in self.entries]

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe document for ``--json``."""
        return {
            "provider": self.provider,
            "models": [entry.to_dict() for entry in self.entries],
            "ids": self.ids(),
            "sources": list(self.sources),
            "refreshed": self.refreshed,
            "refresh_attempted": self.refresh_attempted,
            "reason": self.reason,
            "count": len(self.entries),
        }


def _recent_models() -> List[str]:
    """Models this session has used, newest first. Never raises.

    Read from the conversation snapshot's own ``model_profile`` - the session's
    record of the model it is on - and from any ``recent_models`` the settings
    carry. There is no guess and no provider call: an absent source contributes
    nothing, which is why the Recent group simply does not render on a fresh
    install instead of showing something invented.
    """
    out: List[str] = []
    try:
        from cli import session as _session

        snapshot = _session.load_latest_session() or {}
        profile = snapshot.get("model_profile")
        if isinstance(profile, Mapping):
            value = str(profile.get("model") or "").strip()
            if value:
                provider = str(profile.get("provider") or "").strip()
                out.append(f"{provider}/{value}" if provider else value)
        elif isinstance(profile, str) and profile.strip():
            out.append(profile.strip())
    except Exception:
        pass
    return out


def _read_settings(repo: Optional[str] = None) -> Dict[str, Any]:
    """Read the merged settings plus the session's recent model. Never raises."""
    settings: Dict[str, Any] = {}
    try:
        from cli import neoconfig

        settings = dict(neoconfig.merged_settings())
    except Exception:
        settings = {}
    recent = _recent_models()
    if recent:
        settings["recent_models"] = list(recent)
    return settings


def _configured_providers(settings: Mapping[str, Any]) -> List[str]:
    """Provider names this install knows, for the catalog's own grouping."""
    out: List[str] = []
    declared = str(settings.get("provider") or "").strip()
    if declared:
        out.append(declared)
    profiles = settings.get("provider_profiles")
    if isinstance(profiles, Mapping):
        for name in sorted(profiles):
            profile = profiles.get(name)
            if isinstance(profile, Mapping):
                value = str(profile.get("provider") or "").strip()
                if value:
                    out.append(value)
    for entry in _try_registry_rows():
        if entry.provider:
            out.append(entry.provider)
    return sorted(set(out))


def _try_registry_rows() -> List[ModelEntry]:
    """The capability registry's known rows, or an empty list."""
    module = _mc()
    known = getattr(module, "known_capabilities", None) if module else None
    if not callable(known):
        return []
    try:
        return [
            ModelEntry(
                model=str(getattr(row, "model", "")),
                provider=str(getattr(row, "provider", "")),
                sources=("registry",),
            )
            for row in known()
            if getattr(row, "model", "")
        ]
    except Exception:  # pragma: no cover - defensive
        return []


def _credential_configured(settings: Mapping[str, Any]) -> bool:
    """Whether a provider credential exists, by NAME only.

    A discovery command asks "is there a key configured?", which is a boolean.
    The key itself is never read, and never printed: the answer is a boolean
    and the reason line names the VARIABLE, not its value.
    """
    import os

    names = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "TOKENROUTER_API_KEY",
        "AGENTROUTER_API_KEY",
    )
    return any(str(os.environ.get(name) or "").strip() for name in names) or bool(
        str(settings.get("api_key") or "").strip()
    )


def discover_models(
    provider: Optional[str] = None,
    *,
    refresh: bool = False,
    settings: Optional[Mapping[str, Any]] = None,
    live_probe: Optional[Any] = None,
) -> ModelDiscovery:
    """List the models this install can name, optionally filtered by provider.

    Offline by default and complete offline: the list is built from the settings
    chain, the router's tier table, the named provider profiles and the
    capability registry - the same rows the picker shows, so a person can grep
    for exactly what the picker would offer.

    ``refresh=True`` re-reads every source from disk and then ATTEMPTS a live
    provider probe. The attempt is reported honestly: without a configured
    credential it is not made at all, and the receipt says
    ``refresh_attempted: false`` with the reason. A discovery command that
    prints a local table as though a provider had answered it is the same class
    of lie as a receipt claiming a parameter was sent.
    """
    effective = dict(settings) if isinstance(settings, Mapping) else _read_settings()
    entries = model_catalog(effective)
    wanted = str(provider or "").strip().lower()

    sources = sorted({source for entry in entries for source in entry.sources})
    if wanted:
        entries = [
            entry
            for entry in entries
            if entry.provider.lower() == wanted or entry.model.lower() == wanted
        ]

    attempted = False
    refreshed = False
    reason = ""
    if refresh:
        attempted = True
        if not _credential_configured(effective):
            reason = (
                "local sources re-read; no provider credential is configured, so no "
                "live model list was requested"
            )
        else:
            extra: List[str] = []
            if callable(live_probe):
                try:
                    extra = [str(item) for item in (live_probe(provider) or [])]
                    refreshed = True
                    reason = "live provider model list requested"
                except Exception as exc:  # pragma: no cover - needs a provider
                    reason = f"the live provider model list failed: {exc}"
            else:
                reason = (
                    "local sources re-read; this build does not carry a live "
                    "provider probe, so no remote list was requested"
                )
            for value in extra:
                prov, model = _split_id(value, effective.get("provider"))
                entries.append(
                    ModelEntry(model=model, provider=prov, sources=("live",))
                )
    if not refresh:
        reason = "offline: settings chain, router tiers, provider profiles, registry"

    # Every printed row is a `provider/model` pair. A registry row that declares
    # no provider borrows the CONFIGURED one, and failing that says
    # `unprefixed/` - a word this install means, not a provider name it invented.
    configured = str(effective.get("provider") or "").strip()
    qualified = tuple(
        entry
        if entry.provider
        else ModelEntry(
            model=entry.model,
            provider=configured or "unprefixed",
            sources=entry.sources,
            hint=entry.hint,
        )
        for entry in entries
    )
    return ModelDiscovery(
        entries=qualified,
        provider=str(provider or ""),
        sources=tuple(sources),
        refreshed=refreshed,
        refresh_attempted=attempted,
        reason=reason,
    )


def cmd_models(args: Any, *, discovery: Optional[Any] = None) -> int:
    """``neo models [provider] [--refresh] [--json]``. Returns an exit code.

    Prints one ``provider/model`` per line, which is the shape a person pipes
    into something else. Exit 0 on a listing (including an empty one - "you
    have no models configured" is an answer, not a failure) and 2 on a usage
    error. Output is PLAIN text on stdout so ``--json`` stays exactly one
    document.

    ``discovery`` is the injectable seam, so a test can drive the command
    against a fixed catalog without touching the developer's real settings.
    """
    provider = getattr(args, "provider", None)
    if provider is None:
        provider = getattr(args, "provider_filter", None)
    refresh = bool(getattr(args, "refresh", False))
    as_json = bool(getattr(args, "json", False))
    runner = discovery or discover_models
    try:
        found = runner(str(provider or "").strip() or None, refresh=refresh)
    except Exception as exc:  # discovery must never traceback
        print(f"model discovery failed: {exc}")
        return 2
    if as_json:
        import json

        print(json.dumps(found.to_dict(), indent=2, sort_keys=True, default=str))
        return 0
    for line in found.ids():
        print(line)
    if not found.entries:
        print("(no models are configured; type one in the picker to use it)")
    if refresh:
        print(f"# refresh: {found.reason}")
    return 0


def register_models_parser(sub: Any) -> Any:
    """Add the ``models`` subcommand to an existing subparsers action.

    Lives here rather than in ``cli/main.py`` so the picker, the variant and
    the discovery command are ONE module, and the mount is a single line in
    ``build_parser``:

        from cli import models as _models
        _models.register_models_parser(sub)

    Returns the parser it created, so a caller can extend it; returns ``None``
    when ``sub`` does not look like an argparse subparsers action rather than
    raising, because a mount point that explodes takes the whole CLI with it.
    """
    if sub is None or not hasattr(sub, "add_parser"):
        return None
    try:
        parser = sub.add_parser(
            "models",
            help="list the models this install can use",
            description=(
                "List the selectable models as provider/model. --refresh re-reads "
                "the local sources and attempts a live provider model list."
            ),
        )
        parser.add_argument(
            "provider",
            nargs="?",
            default=None,
            help="only list this provider (or this exact model id)",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="re-read every source and attempt a live provider model list",
        )
        parser.add_argument(
            "--json", action="store_true", help="machine-readable discovery report"
        )
        parser.set_defaults(func=cmd_models)
    except Exception:  # pragma: no cover - a broken argparse shape
        return None
    return parser


# ---------------------------------------------------------------------------
# Markup safety
# ---------------------------------------------------------------------------
#
# Rule: a render failure must NEVER delete a message. The failure mode is
# specific - a string that crossed into a markup parser carrying a `[...]`
# sequence is consumed as a tag, so a model named `[red]evil[/red]` or a
# provider detail quoting `[/]` takes the surrounding text with it. Every
# renderer here therefore returns PLAIN lines, and the two helpers below are the
# only sanctioned ways out of this module.


def plain_lines(lines: Iterable[Any]) -> List[str]:
    """Coerce to plain strings, dropping nothing.

    A value whose ``__str__`` raises becomes the empty string rather than
    taking the whole render down: a row nobody can name is still a row, and a
    crash in a picker is worse than a blank label.
    """
    out: List[str] = []
    for line in lines or ():
        try:
            out.append("" if line is None else str(line))
        except Exception:  # pragma: no cover - a __str__ that raises
            out.append("")
    return out


def escape_lines(lines: Iterable[Any]) -> List[str]:
    """Escape each line for a markup renderer.

    The bracket-escaping is applied with :func:`rich.markup.escape` when rich
    is importable, so the escaping is the renderer's OWN and cannot drift from
    it; without rich the same characters are escaped by hand. Either way a
    hostile model id stays VISIBLE - escaping a message is not the same as
    dropping it.
    """
    try:
        from rich.markup import escape as _rich_escape

        return [_rich_escape(line) for line in plain_lines(lines)]
    except Exception:  # pragma: no cover - a path with no rich
        return [line.replace("[", r"\[") for line in plain_lines(lines)]


def safe_lines(lines: Iterable[Any]) -> List[Any]:
    """Return ``rich.text.Text`` objects, which no markup parser can eat.

    The structural answer, and the one a surface should prefer: a ``Text`` has
    no markup interpretation at all, so a model id containing ``[/]`` is
    rendered rather than swallowed. Falls back to the escaped strings when rich
    is unavailable, so the helper never itself becomes the crash.
    """
    plain = plain_lines(lines)
    try:
        from rich.text import Text

        return [Text(line) for line in plain]
    except Exception:  # pragma: no cover - a path with no rich
        return escape_lines(plain)
