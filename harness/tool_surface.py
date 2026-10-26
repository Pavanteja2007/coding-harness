"""Per-model-family tool surfaces, keyed on a DECLARED capability.

The problem, and the two failure directions
-------------------------------------------
OpenCode swaps the tool surface per model family: GPT-family models get a port
of Codex's ``apply_patch`` DSL and **lose** ``edit``/``write`` entirely. Aider
does the same with 13 registered edit formats and a prompt factory that selects
from model metadata.

Our state before this module: ``apply_patch`` exists and is available to every
model **alongside** ``edit``/``write``. All shaping was per-STRATEGY and per-MODE
(:func:`harness.agent_kernel.subagents.plan_mode_tools`, ``runtime/roles.py``
role profiles), never per-MODEL. :func:`runtime.model_capabilities.lookup_capability`
has ``supports_tools``, but that is a **routing filter** - "do not route a tool
task to a model that cannot call tools" - which is a different question from
"which tools should this model be offered".

**Both mistakes are failures and they fail in opposite directions:**

* **UNDER-provision** - offering ``edit``/``write`` to a model whose edit
  training omits them produces confidently wrong patches, because the model
  knows the shape of an edit and not this agent's argument names.
* **OVER-provision** - withholding a tool a model needs is strictly worse than
  the status quo, because the model cannot express the work at all and will
  burn turns improvising around the hole.

:func:`surface_violations` checks both, in one pass, and names which kind it
found. A mapping that only ever tested for over-provisioning would ship the
under-provisioning defect silently.

Why this is OPT-IN
------------------
**Nothing is removed by default.** An absent ``tool_surface_capabilities`` key
means "no mapping applies" and every model receives the full catalog, which is
exactly today's behaviour. This is the same rule as AGT-13's
key-presence-plus-truthiness and R2-03's ``test_config_change``: a mapping that
silently disabled ``edit`` for a model family would be a behaviour change
nobody asked for and nobody could debug.

The opt-in is keyed on a **declared capability** rather than on a model-name
string, for the same reason T3's :func:`runtime.model_capabilities.effort_family_for`
resolves a family: an ``openai``-named gateway fronts Anthropic and Google
models too, so a hand-written ``if "gpt" in model:`` if-chain picks the wrong
surface for a Claude model served through it. Family resolution is therefore
DELEGATED to T3, never re-derived here.

Why the ledger records the choice
---------------------------------
:meth:`SurfaceLedger.explain` states the surface, the family it came from, which
capability declared it, what it withheld and why. "The model did not use
``edit``" is unanswerable from a trace; "``edit`` was withheld because
``tool_surface_capabilities`` declared ``gpt_codex_patch`` for this model" is
answerable. The prompt is also told the actual surface, because a prompt that
advertises tools the schema does not carry teaches the model to call tools that
do not exist.

The capability vocabulary
-------------------------
A declared capability is a string naming a SHAPE, not a product:

``gpt_codex_patch``
    The model is trained on the Codex ``apply_patch`` dialect and should be
    offered ``apply_patch`` **instead of** ``edit``/``write``.

``prefer_apply_patch``
    Offer both, and say which to prefer in the prompt. A weaker, additive
    variant: it never withholds a tool.

Anything else is refused by name. A capability nobody implements must not
silently mean "no mapping", or a typo disables every override in the config and
the receipt cannot tell why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CAP_GPT_CODEX_PATCH",
    "CAP_PREFER_APPLY_PATCH",
    "MODE_FULL",
    "MODE_PATCH_ONLY",
    "MODE_PREFER_PATCH",
    "PATCH_REPLACEMENT",
    "SURFACE_CAPABILITIES",
    "SURFACE_MODES",
    "SurfaceLedger",
    "surface_for",
    "surface_violations",
    "truthiness_of",
]


# -- declared capabilities --------------------------------------------------

#: Offer ``apply_patch`` INSTEAD of ``edit``/``write``. Withholding is the
#: point of this capability and the reason it is opt-in.
CAP_GPT_CODEX_PATCH = "gpt_codex_patch"

#: Offer both, and tell the model to prefer ``apply_patch``. **Never withholds.**
CAP_PREFER_APPLY_PATCH = "prefer_apply_patch"

SURFACE_CAPABILITIES: Tuple[str, ...] = (CAP_GPT_CODEX_PATCH, CAP_PREFER_APPLY_PATCH)

# -- the two shapes a surface can have --------------------------------------

MODE_FULL = "full"
MODE_PATCH_ONLY = "patch_only"
MODE_PREFER_PATCH = "prefer_patch"

SURFACE_MODES: Tuple[str, ...] = (MODE_FULL, MODE_PATCH_ONLY, MODE_PREFER_PATCH)

#: The tools ``patch_only`` withholds. Named once so the prompt renderer, the
#: ledger, and the violation check cannot disagree about what "withheld" means.
_WITHHELD_BY_PATCH_ONLY: Tuple[str, ...] = ("edit", "write")


def truthiness_of(value: Any) -> bool:
    """Whether a config value is a TRUTHY declaration, read by key meaning.

    AGT-13's rule, reused rather than reinvented: ``"0"``, ``"false"``, ``"no"``
    and ``"off"`` are all FALSE despite being non-empty strings, and a typo
    cannot enable an override. A list/dict is true when non-empty; ``None``,
    numbers and everything else fall back to ``bool()``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return bool(value)


@dataclass(frozen=True)
class SurfaceLedger:
    """Which surface a model received, which family decided it, and what it cost.

    Frozen and additive: this rides the run's own receipt rather than replacing
    anything, and every field is optional at the edges so a caller that builds
    one by hand (a test, a replay) cannot be forced to invent a provenance.
    """

    mode: str = MODE_FULL
    family: str = ""
    model: str = ""
    provider: str = ""
    #: The declared capability that produced this surface, or ``""`` for the
    #: default full surface.
    capability: str = ""
    #: Where the capability came from. ``""`` means "none declared".
    source: str = ""
    withheld: Tuple[str, ...] = field(default_factory=tuple)
    added: Tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    total: int = 0
    unknown_capabilities: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def withheld_names(self) -> Tuple[str, ...]:
        return tuple(self.withheld)

    @property
    def default(self) -> bool:
        """Whether this is the untouched full surface (no mapping applied)."""
        return self.mode == MODE_FULL and not self.capability

    def names(self, catalog: Sequence[str]) -> List[str]:
        """The concrete tool names this surface offers, from ``catalog``."""
        allowed = set(catalog)
        keep = [
            name for name in catalog if name not in self.withheld or name in self.added
        ]
        return [name for name in keep if name in allowed]

    def prompt_note(self) -> str:
        """The model-facing line describing the ACTUAL surface.

        Empty for the default surface, so an unconfigured run's prompt is
        byte-identical to its pre-round self - the same discipline
        ``agent_default_strategy`` followed, and the reason no eval arm has to
        diff a prompt this round did not change for most models.
        """
        if self.default:
            return ""
        if self.mode == MODE_PATCH_ONLY:
            return (
                f"[tool surface: {self.capability}] Use `apply_patch` for every "
                f"file change. `edit` and `write` are not available in this "
                f"session - do not call them."
            )
        if self.mode == MODE_PREFER_PATCH:
            return (
                f"[tool surface: {self.capability}] Prefer `apply_patch` for "
                f"file changes. `edit` and `write` remain available."
            )
        return ""

    def explain(self) -> Dict[str, Any]:
        """The publishable receipt: the surface, the why, and the withheld names."""
        return {
            "surface_mode": self.mode,
            "surface_family": self.family,
            "surface_model": self.model,
            "surface_provider": self.provider,
            "surface_capability": self.capability,
            "surface_source": self.source,
            "surface_withheld": list(self.withheld),
            "surface_added": list(self.added),
            "surface_total": int(self.total),
            "surface_default": self.default,
            "surface_reason": self.reason,
            "surface_unknown_capabilities": list(self.unknown_capabilities),
        }


def _declared_capabilities(config: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """Read the opt-in key by PRESENCE + truthiness, per AGT-13.

    The key may be a single string or a list. An absent key, ``None``, ``""``,
    ``False`` and ``"off"`` all mean "no mapping applies", and therefore "every
    model keeps the full catalog".

    **The truthiness rule applies to SCALARS only.** A non-empty LIST of
    capability names is a declaration, not an "on" switch - and this round's own
    first draft got that wrong, treating a non-empty list as truthy and
    therefore as "no mapping", so ``["prefer_apply_patch"]`` silently kept the
    full surface. A list's emptiness is the OFF arm; a string's truthiness is.
    Applying one truthiness rule to two different shapes is how a config key
    ends up meaning neither.
    """
    if not config or "tool_surface_capabilities" not in config:
        return ()
    raw = config.get("tool_surface_capabilities")
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(item).strip().lower() for item in raw if str(item).strip())
    if truthiness_of(raw):
        return ()
    if isinstance(raw, str):
        items: Iterable[Any] = (part for part in raw.replace(";", ",").split(","))
    else:
        return ()
    return tuple(str(item).strip().lower() for item in items if str(item).strip())


def _family_for(model: Optional[str], provider: Optional[str]) -> str:
    """The model FAMILY, delegated to T3. See the module docstring.

    Returns ``""`` when the family cannot be resolved, which selects no
    mapping - the safe direction, since every mapping here either withholds a
    tool or changes the prompt's recommendation, and neither should happen on a
    guess.
    """
    try:
        from runtime.model_capabilities import effort_family_for
    except Exception:  # pragma: no cover - import guard
        return ""
    try:
        return str(effort_family_for(model, provider) or "")
    except Exception:
        return ""


def surface_for(
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    catalog: Optional[Sequence[str]] = None,
) -> SurfaceLedger:
    """Return the tool surface for one model. **Never withholds by default.**

    ``catalog`` is the full set of available tool names. It is used to report
    ``total`` and to validate that a withheld name is real, so a mapping cannot
    report withholding a tool that does not exist - a receipt naming a tool the
    model never had is the kind of false claim this module exists to prevent.
    """
    available = tuple(str(name) for name in (catalog or ()))
    declared = _declared_capabilities(config)
    base = SurfaceLedger(
        model=str(model or ""),
        provider=str(provider or ""),
        family=_family_for(model, provider),
        total=len(available),
    )
    if not declared:
        return base

    known = [name for name in declared if name in SURFACE_CAPABILITIES]
    unknown = tuple(name for name in declared if name not in SURFACE_CAPABILITIES)
    if not known:
        # A capability nobody implements must NOT quietly mean "no mapping" in a
        # way the receipt cannot explain. Say so, and keep the full surface.
        return SurfaceLedger(
            model=base.model,
            provider=base.provider,
            family=base.family,
            source="tool_surface_capabilities",
            reason=(
                f"declared capabilities {list(unknown)} are not implemented; "
                f"the full surface was kept"
            ),
            total=len(available),
            unknown_capabilities=unknown,
        )

    capability = known[0]
    if capability == CAP_GPT_CODEX_PATCH:
        mode = MODE_PATCH_ONLY
        withheld = _WITHHELD_BY_PATCH_ONLY
        if available:
            # Only report withholding what actually exists.
            withheld = tuple(name for name in withheld if name in available)
    else:
        # `prefer_apply_patch` is the ADDITIVE variant. It must report an empty
        # `withheld`, and this branch exists because the first draft computed
        # `withheld` unconditionally above it - so a capability whose entire
        # purpose is withholding nothing reported withholding two tools. A
        # receipt that names a tool the model still has is worse than a receipt
        # that names nothing.
        mode = MODE_PREFER_PATCH
        withheld = ()
    return SurfaceLedger(
        mode=mode,
        family=base.family,
        model=base.model,
        provider=base.provider,
        capability=capability,
        source="tool_surface_capabilities",
        withheld=withheld,
        reason=(f"{capability!r} declared for family {base.family or 'unresolved'!r}"),
        total=len(available),
        unknown_capabilities=unknown,
    )


#: The replacement a ``patch_only`` surface MUST offer. Withholding ``edit``
#: and ``write`` is only defensible if something else can change a file, and
#: ``apply_patch`` is that something. This is the concrete statement of the
#: under-provisioning rule: a surface that removes every mutation tool without
#: naming a replacement leaves the model unable to do the job, which is strictly
#: worse than the status quo.
PATCH_REPLACEMENT = "apply_patch"


def surface_violations(
    ledger: SurfaceLedger,
    offered: Sequence[str],
) -> List[Dict[str, str]]:
    """Return BOTH classes of mis-provisioning, each named.

    This is the test the brief asks for, as a function a test can call: a
    mapping that offers a withheld tool (``over_provisioned``) and a mapping that
    fails to replace what it withheld (``under_provisioned``) are both failures,
    and a check that only looked for one of them would pass the other's defect.

    ``over_provisioned`` - a tool the ledger says it withheld is nonetheless in
    the offered surface. The receipt and the schema disagree, and the receipt is
    the one a reader trusts.

    ``under_provisioned`` - a ``patch_only`` surface withheld ``edit``/``write``
    and did not offer :data:`PATCH_REPLACEMENT`. The model now has no way to
    change a file at all. This is why the two checks share one function: they
    are the two ends of one requirement, and testing one does not test the
    other.
    """
    names = {str(name) for name in offered or ()}
    problems: List[Dict[str, str]] = []
    for name in ledger.withheld:
        if name in names:
            problems.append(
                {
                    "kind": "over_provisioned",
                    "tool": name,
                    "detail": (
                        f"{name!r} was withheld by {ledger.capability!r} but is "
                        f"in the offered surface"
                    ),
                }
            )
    if ledger.withheld and PATCH_REPLACEMENT not in names:
        problems.append(
            {
                "kind": "under_provisioned",
                "tool": PATCH_REPLACEMENT,
                "detail": (
                    f"{ledger.capability!r} withheld "
                    f"{list(ledger.withheld)} without offering "
                    f"{PATCH_REPLACEMENT!r}; the model cannot change any file"
                ),
            }
        )
    return problems
