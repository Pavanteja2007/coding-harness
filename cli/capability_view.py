"""What the agent can reach right now — and WHY it is not reaching it.

Three states, not two, and the third is the one this module exists for
----------------------------------------------------------------------
Phase 3b makes the agent's capability set dynamic. A surface that answers
"available / unavailable" now answers the wrong question, because three
different things hide a tool:

===============  =========================================  ====================
state            means                                     user-facing
===============  =========================================  ====================
``available``    in the prompt now                          shown
``deferred``     NOT in the prompt, discoverable by search  shown as *deferred*,
                                                             with HOW to find it
``withheld``     absent by strategy or policy               shown as *withheld*,
                                                             **with the reason**
===============  =========================================  ====================

"An unexplained withheld tool is indistinguishable from a broken one", and a
*deferred* tool that is merely listed reads as a missing one. That last case is
the harm this phase can cause: **the agent says it cannot do something it
actually could.** The user's response to that is to switch models or give up,
when the real cause is a search nobody ran.

So the rules this module keeps, each with a function a test can call:

1. :func:`tool_capability_report` produces the three states from the REAL
   authorities — never from a hand-kept table. Withholding comes from
   :mod:`cli.approval_modes` (mode + policy) and :mod:`harness.tool_surface`
   (per-model-family). A second copy of either is the failure class this
   repository has paid for repeatedly.
2. **Every withheld row carries a reason.** A withheld row with an empty
   reason is refused by :func:`assert_no_false_capability` rather than
   rendered.
3. **Every deferred row says how to find it.** :data:`DEFERRED_HOW` is the
   affordance text, and it is not optional.
4. :func:`assert_no_false_capability` is the INVERSION gate: it fails when a
   tool the model was actually offered is rendered as withheld or deferred,
   and when a withheld tool is rendered with no reason. This is the only gate
   in the module that is allowed to raise, and it is the one a reader of the
   receipt would want. It carries a control arm requirement — a gate that
   cannot fire is worse than an absent one.

Honest absence, everywhere
---------------------------
Nothing here invents a capability and nothing here hides one:

* no catalog -> ``catalog_available: False`` with a reason, and the view says
  so instead of rendering an empty list that reads like a machine with no
  tools;
* no deferred registry declared -> ``deferred_source: "none declared"`` and a
  zero that is labelled as a zero;
* a policy that withholds everything -> a non-empty report that says the mode
  is why, not a blank screen.

The deferred registry does not exist in ``harness`` yet, so it is read from
three honest sources and the receipt names which ones fired:
``config["deferred_tools"]`` (read by key PRESENCE — a value in
``harness/config.py::DEFAULTS`` merges into every task and every eval arm, so
nothing is published here), the ``tool_search`` journal events a run recorded,
and the declared model-family surface. That is a **filed gap**, not a design:
see ``cli/AGENTS.md``.

What is sanitised, and why it is a new surface
----------------------------------------------
A tool *description* is untrusted content: an MCP server, a plugin, or a
third-party skill directory supplies it, and it lands on a terminal. So every
description crosses :func:`cli.ui.sanitize_text` at the render boundary, and
:func:`assert_descriptions_are_sanitised` checks that the render path cannot
be reached without it. This is a NEW injection surface introduced by showing
deferred and withheld tools with their descriptions, which is why it is pinned
rather than assumed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CAPABILITY_GAP_HOW",
    "DEFERRED_HOW",
    "DEFERRED_SOURCES",
    "GAP_MARKERS",
    "TOOL_STATES",
    "WITHHELD_REASONS",
    "CapabilityReport",
    "CapabilityRow",
    "DiscoverabilityMiss",
    "assert_descriptions_are_sanitised",
    "assert_no_false_capability",
    "capability_gap_hint",
    "capability_lines",
    "catalog_names",
    "deferred_names",
    "discoverability_lines",
    "inability_claim",
    "load_discoverability",
    "record_discoverability_miss",
    "tool_capability_report",
    "tool_search_lines",
    "tool_search_projection",
    "turn_composition_lines",
    "turn_composition_projection",
]

#: The three states, in the order a reader meets them. A closed set: a fourth
#: state would be a fourth vocabulary for "the agent cannot do this", which is
#: the failure mode.
TOOL_STATES: Tuple[str, ...] = ("available", "deferred", "withheld")

#: Why a tool is withheld. ``mode`` and ``policy`` are the two this module can
#: always explain; the rest are reported when the authority names them, and an
#: unrecognised reason is normalised to ``unknown`` rather than dropped — a
#: reason we do not understand is still a reason we must show.
WITHHELD_REASONS: Tuple[str, ...] = (
    "mode",
    "policy",
    "model_family_surface",
    "declared_internal",
    "unknown",
)

#: Where the deferred set came from. Named so a zero can be read: "nothing is
#: deferred" and "no authority declared anything deferred" are different facts
#: and only one of them is good news.
DEFERRED_SOURCES: Tuple[str, ...] = (
    "none",
    "config",
    "search_event",
    "config+search_event",
    "model_family_surface",
)

#: How a reader finds a deferred tool. This is an AFFORDANCE, so it is a
#: constant rather than prose at the call site: a hint that is retyped is a
#: hint that goes stale.
DEFERRED_HOW = "not in this prompt - ask the agent to search its tools by name"

#: What the user is told when the agent claims it cannot do something a
#: deferred tool covers. The single highest-value sentence this phase ships.
CAPABILITY_GAP_HOW = (
    "there is a deferred tool for this - ask the agent to search its tools "
    "by name before concluding it cannot"
)

#: Phrases an assistant uses when it claims a limitation. Deliberately a
#: closed, SMALL list: a broad "does this look like refusal" matcher would
#: fire on ordinary prose and the discoverability ledger would fill with
#: nonsense. These are the shapes that read as a capability claim.
GAP_MARKERS: Tuple[str, ...] = (
    "i cannot",
    "i can't",
    "i can not",
    "i do not have",
    "i don't have",
    "i'm not able",
    "i am not able",
    "not available to me",
    "no tool for",
    "no tool that",
    "there is no tool",
    "there isn't a tool",
    "no access to",
    "not supported in this build",
    "unable to access",
    "cannot access",
)

#: Name fragments that carry no signal in a claim. Short, and only the ones
#: that actually occur in this catalog — a long stopword list is a list
#: somebody will extend to silence a real signal.
_NAME_STOPWORDS = frozenset({"get", "run", "set", "the", "and", "for", "with", "tool", "call"})

#: Upper bound on how many discoverability misses one session keeps. Bounded
#: because an object store with no GC grows without bound, and a ledger that
#: grows forever is a ledger nobody reads.
MAX_DISCOVERABILITY_MISSES = 256

#: Where the misses live, relative to a run directory. Under the run, beside
#: the journal, so a run's evidence carries its own diagnostic and nothing has
#: to guess which file to open.
DISCOVERABILITY_FILENAME = "discoverability.jsonl"


# ---------------------------------------------------------------------------
# The catalog — the real one, or an honest absence
# ---------------------------------------------------------------------------


def catalog_names() -> Tuple[Tuple[str, ...], str]:
    """Return ``(tool names, reason)`` from the ONE canonical catalog.

    Delegates to :func:`harness.tools.canonical_tool_names`. A catalog this
    build cannot import is reported as an empty tuple **with a reason**, never
    as an empty machine: "no tools" and "we could not read the catalog" are
    different facts and the view must not collapse them.
    """
    try:
        from harness.tools import canonical_tool_names
    except Exception as exc:  # pragma: no cover - import guard
        return (), f"tool catalog unavailable: {type(exc).__name__}"
    try:
        names = tuple(str(name) for name in canonical_tool_names())
    except Exception as exc:
        return (), f"tool catalog unreadable: {type(exc).__name__}"
    if not names:
        return (), "tool catalog is empty in this build"
    return names, ""


def _catalog_specs() -> Tuple[Tuple[Any, ...], str]:
    """Return the catalog SPECS, which carry descriptions and effect classes."""
    try:
        from harness.tools import typed_tool_specs
    except Exception as exc:  # pragma: no cover - import guard
        return (), f"tool catalog unavailable: {type(exc).__name__}"
    try:
        return tuple(typed_tool_specs()), ""
    except Exception as exc:
        return (), f"tool catalog unreadable: {type(exc).__name__}"


def _spec_descriptions() -> Dict[str, str]:
    """``tool -> description`` from the catalog, best effort.

    A description is DATA from a catalog that plugin and MCP contributions can
    extend, so it is treated as untrusted everywhere it is rendered.
    """
    specs, _reason = _catalog_specs()
    out: Dict[str, str] = {}
    for spec in specs:
        name = str(getattr(spec, "name", "") or "")
        if not name:
            continue
        out[name] = str(getattr(spec, "description", "") or "")
    return out


# ---------------------------------------------------------------------------
# The deferred set — read by key presence, never published as a default
# ---------------------------------------------------------------------------


def deferred_names(
    config: Optional[Mapping[str, Any]] = None,
    *,
    searched: Iterable[str] = (),
) -> Tuple[Tuple[str, ...], str]:
    """Return ``(deferred names, source)``.

    Three sources, named in the return value:

    * ``config["deferred_tools"]`` — read by key **PRESENCE**. The rule is the
      repository's: a value in ``harness/config.py::DEFAULTS`` is merged into
      every task and every eval arm, so a deferred set published there would
      silently change every run in the project. ``None``, ``""`` and ``False``
      all mean "nothing declared", which is different from an empty list
      meaning "everything is deferred".
    * ``searched`` — names a run's ``tool_search`` events say it looked for
      and did not find in the prompt. That is a MEASURED deferral: the agent
      proved it could not see the tool.
    * the model-family surface, handled by the caller because it needs the
      ledger.

    An unrecognised config value is reported as empty **with the reason on the
    receipt**, never coerced into a set: a typo that silently deferred nothing
    would make the whole capability view a lie.
    """
    from_config: Tuple[str, ...] = ()
    source_parts: List[str] = []
    if isinstance(config, Mapping) and "deferred_tools" in config:
        raw = config.get("deferred_tools")
        if isinstance(raw, (list, tuple, set, frozenset)):
            from_config = tuple(
                sorted({str(item).strip() for item in raw if str(item).strip()})
            )
            source_parts.append("config")
        elif raw in (None, False, ""):
            source_parts.append("config")
        elif isinstance(raw, str):
            from_config = tuple(
                sorted({part.strip() for part in raw.replace(";", ",").split(",") if part.strip()})
            )
            source_parts.append("config")
        else:
            # Named, not silently ignored: an unusable value is a fact.
            return (), "deferred_tools is not a list of names; nothing read"
    found = tuple(sorted({str(name).strip() for name in searched if str(name).strip()}))
    if found:
        source_parts.append("search_event")
    names = tuple(sorted(set(from_config) | set(found)))
    if not source_parts:
        return (), "none"
    return names, "+".join(source_parts)


def _model_family_withheld(
    config: Optional[Mapping[str, Any]],
    names: Sequence[str],
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> Tuple[Tuple[str, ...], str]:
    """Names withheld by the DECLARED per-model-family surface, plus a reason.

    Delegates to :mod:`harness.tool_surface`, whose own ledger says which
    capability produced the surface. We re-use it rather than re-deriving it:
    two answers to "which tools does this model get" is the defect this
    repository records most often.

    ``ledger.default`` means the untouched full surface, so it returns nothing —
    "this model gets everything" is not a withholding and rendering it as one
    would invent a capability gap that does not exist.
    """
    try:
        from harness.tool_surface import surface_for
    except Exception:
        return (), ""
    try:
        ledger = surface_for(
            model=model,
            provider=provider,
            config=config,
            catalog=list(names),
        )
    except Exception as exc:  # pragma: no cover - defensive
        return (), f"model-family surface unavailable: {type(exc).__name__}"
    if ledger.default or not ledger.withheld:
        return (), ""
    reason = str(ledger.reason or "")
    if not reason and ledger.capability:
        reason = f"the {ledger.capability!r} capability for this model family withholds it"
    return tuple(ledger.withheld_names), reason


# ---------------------------------------------------------------------------
# The rows and the report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityRow:
    """One tool, its state, and the sentence that explains the state."""

    name: str
    state: str
    reason: str = ""
    #: ``deferred`` only: how a reader finds it. Never empty on a deferred row.
    how: str = ""
    description: str = ""
    #: WHICH authority produced this state, from :data:`WITHHELD_REASONS`.
    #: Separate from ``reason`` because the reason is prose and this is a word
    #: a reader can count and a gate can assert; two answers to one question is
    #: how a vocabulary rots.
    category: str = ""
    #: ``None`` when nothing declared the question. ``False`` is the honest and
    #: important half: a withheld tool that is ALSO not dispatchable really is
    #: unreachable, and a receipt that says only "withheld" would let a reader
    #: assume it might still work.
    dispatchable: Optional[bool] = None
    #: Why, when ``dispatchable`` is False. Empty when nothing declared it.
    dispatch_reason: str = ""
    #: ``True`` when this tool is withheld AND also named as deferred. Both
    #: facts are real and the withheld reason is the one that decides the
    #: state, so the deferral rides along rather than being dropped: a ledger
    #: that silently loses a row because two authorities agreed is a ledger
    #: that under-counts exactly the cases worth counting.
    deferred_also: bool = False
    #: ``True`` when the mode surface says the MODEL can invoke this tool and
    #: a deferral says it is not in the prompt. Two authorities, one answer
    #: wanted, and the disagreement is the finding — see
    #: :meth:`CapabilityReport.conflicts`.
    conflict: bool = False
    conflict_detail: str = ""

    @property
    def deferred(self) -> bool:
        return self.state == "deferred"

    @property
    def withheld(self) -> bool:
        return self.state == "withheld"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.name,
            "state": self.state,
            "reason": self.reason,
            "how": self.how,
            "description": self.description,
            "category": self.category,
            "dispatchable": self.dispatchable,
            "dispatch_reason": self.dispatch_reason,
            "deferred_also": self.deferred_also,
            "conflict": self.conflict,
            "conflict_detail": self.conflict_detail,
        }


@dataclass(frozen=True)
class CapabilityReport:
    """Every tool, its state, the counts, and where the deferred set came from."""

    rows: Tuple[CapabilityRow, ...] = ()
    mode: str = ""
    catalog_total: int = 0
    catalog_available: bool = True
    catalog_reason: str = ""
    deferred_source: str = "none"
    counts: Mapping[str, int] = field(default_factory=dict)
    #: What the MODEL was actually offered this turn. The authority for the
    #: inversion check: a tool in here can never be rendered as withheld.
    offered: Tuple[str, ...] = ()

    def of_state(self, state: str) -> Tuple[CapabilityRow, ...]:
        return tuple(row for row in self.rows if row.state == str(state))

    @property
    def available(self) -> Tuple[CapabilityRow, ...]:
        return self.of_state("available")

    @property
    def deferred(self) -> Tuple[CapabilityRow, ...]:
        return self.of_state("deferred")

    @property
    def withheld(self) -> Tuple[CapabilityRow, ...]:
        return self.of_state("withheld")

    def counts_line(self) -> str:
        """``42 of 45 available`` — the at-a-glance form a user wants."""
        total = max(1, int(self.catalog_total or len(self.rows)))
        counts = self.counts or {}
        return (
            f"{int(counts.get('available', 0))} of {total} available "
            f"· {int(counts.get('deferred', 0))} deferred "
            f"· {int(counts.get('withheld', 0))} withheld"
        )

    def false_capabilities(self) -> List[Dict[str, str]]:
        """Every way this report claims a capability the agent HAS is missing.

        The inversion, stated as data so a test can assert on it and a reader
        can print it:

        * a tool in ``offered`` rendered ``withheld`` — the agent HAS it and the
          view says it does not;
        * a tool in ``offered`` rendered ``deferred`` — same inversion in the
          softer spelling, and just as misleading;
        * a ``withheld`` row with no reason — an unexplained hole;
        * a ``deferred`` row with no ``how`` — a hint nobody can act on.

        ``catalog_available: False`` contributes nothing: with no catalog there
        are no rows, and "we could not read the catalog" is not a claim.
        """
        problems: List[Dict[str, str]] = []
        offered = set(self.offered or ())
        conflicting = {row.name for row in self.rows if row.conflict}
        for row in self.rows:
            if row.state not in TOOL_STATES:
                problems.append(
                    {
                        "kind": "unknown_state",
                        "tool": row.name,
                        "detail": f"state {row.state!r} is not one of {list(TOOL_STATES)}",
                    }
                )
                continue
            if row.withheld and not str(row.reason or "").strip():
                problems.append(
                    {
                        "kind": "withheld_without_reason",
                        "tool": row.name,
                        "detail": "a withheld tool with no reason is indistinguishable from a broken one",
                    }
                )
            if row.deferred and not str(row.how or "").strip():
                problems.append(
                    {
                        "kind": "deferred_without_how",
                        "tool": row.name,
                        "detail": "a deferred tool with no way to find it reads as a missing one",
                    }
                )
            if row.withheld and str(row.category or "") not in WITHHELD_REASONS:
                # A category outside the closed set is an authority this build
                # does not understand. It is reported rather than folded into
                # ``unknown``: a reader counting reasons has to be able to
                # count this one.
                problems.append(
                    {
                        "kind": "unknown_withheld_category",
                        "tool": row.name,
                        "detail": (
                            f"category {row.category!r} is not one of {list(WITHHELD_REASONS)}"
                        ),
                    }
                )
            if row.name in offered and row.state != "available" and row.name not in conflicting:
                # A row flagged `conflict` is EXCLUDED here on purpose: two
                # authorities disagreeing is a finding reported by
                # :meth:`conflicts`, and firing the inversion gate on it would
                # make an unfinished feature indistinguishable from a lie. A
                # genuine inversion — nothing declared the conflict — still
                # fires, which is the arm the control tests exercise.
                problems.append(
                    {
                        "kind": "false_capability",
                        "tool": row.name,
                        "detail": (
                            f"{row.name!r} was offered to the model and is rendered "
                            f"{row.state!r}; the agent HAS it"
                        ),
                    }
                )
        return problems

    def conflicts(self) -> List[Dict[str, str]]:
        """Where two authorities DISAGREE about one tool. Never silently resolved.

        The mode surface (:mod:`cli.approval_modes`) computes what the MODEL's
        prompt contains. A deferral is a statement about the same prompt. When
        both name the same tool and say opposite things, one of them is wrong
        and this module cannot tell which — so it says so, per tool, with the
        two claims quoted.

        This is a **finding, not a gate failure**: the report is telling the
        truth about a tree where deferred tools are declared but not yet
        implemented in the surface authority. :func:`assert_no_false_capability`
        treats these separately from a genuine inversion, because a declared
        deferral that nothing implements is an unfinished feature and a UI
        inversion is a lie.
        """
        offered = set(self.offered or ())
        out: List[Dict[str, str]] = []
        for row in self.rows:
            if row.conflict:
                out.append(
                    {
                        "tool": row.name,
                        "surface_says": "offered to the model",
                        "deferral_says": "not in this prompt",
                        "detail": row.conflict_detail,
                    }
                )
            elif row.name in offered and row.deferred:
                out.append(
                    {
                        "tool": row.name,
                        "surface_says": "offered to the model",
                        "deferral_says": "not in this prompt",
                        "detail": "declared deferred by a source the surface does not implement",
                    }
                )
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "catalog_total": int(self.catalog_total),
            "catalog_available": bool(self.catalog_available),
            "catalog_reason": self.catalog_reason,
            "deferred_source": self.deferred_source,
            "counts": {state: int((self.counts or {}).get(state, 0)) for state in TOOL_STATES},
            "counts_line": self.counts_line(),
            "offered": list(self.offered),
            "rows": [row.as_dict() for row in self.rows],
            "false_capabilities": self.false_capabilities(),
            "conflicts": self.conflicts(),
        }


def tool_capability_report(
    *,
    mode: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    denied: Optional[Iterable[Any]] = None,
    deferred: Optional[Iterable[Any]] = None,
    searched: Iterable[str] = (),
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> CapabilityReport:
    """Build the three-state capability report. Never raises.

    ``mode`` is the approval mode (``plan``/``default``/``auto_edit``/
    ``yolo``); ``denied`` is this turn's policy denial set; ``deferred`` and
    ``searched`` are the two ways a caller states the deferred set (the config
    key is read here too, so a caller that has a ``Task.config`` need not know
    the key name).

    Withholding is delegated, never re-derived:
    :func:`cli.approval_modes.visible_surface` for the mode and the policy,
    :func:`harness.tool_surface.surface_for` for the declared model family.
    A withheld row's reason is the authority's own sentence.
    """
    names, reason = catalog_names()
    specs_available = bool(names)
    descriptions = _spec_descriptions() if specs_available else {}

    # -- what the model was actually offered, from the real surface authority --
    visibility = None
    mode_name = ""
    if specs_available:
        try:
            from cli.approval_modes import resolve_mode, visible_surface
            from harness.tools import typed_tool_specs

            # An explicit `mode` argument WINS over the config: a caller that
            # says which mode it is rendering is a caller we must not
            # second-guess, and a silently-ignored argument is the kind of
            # thing a receipt then reports as "changed".
            if mode:
                from cli.approval_modes import normalize_mode

                mode_name = normalize_mode(mode, default="") or str(mode)
            else:
                resolved = resolve_mode(config if config is not None else {})
                # `resolve_mode` returns `(mode, reason)`. Unpack by position:
                # it is a plain tuple, so an attribute read would hand back
                # the pair itself and render as a mode nobody chose.
                if isinstance(resolved, tuple) and len(resolved) >= 2:
                    mode_name = str(resolved[0])
                else:
                    mode_name = str(resolved)
            visibility = visible_surface(mode_name, typed_tool_specs(), denied=denied)
        except Exception:
            visibility = None
    # `model_visible` is the authority for what the MODEL can invoke. The
    # other half — `executable` / `dispatch_blocked_by` — says what survives a
    # refusal, and the two together are what make a withheld row honest: a tool
    # that is hidden but still dispatchable is REACHABLE, and a receipt that
    # calls it missing is the false capability this phase is about.
    offered: Tuple[str, ...] = tuple(visibility.model_visible) if visibility else names
    reasons: Dict[str, str] = dict(visibility.reasons()) if visibility else {}
    blocked_by: Dict[str, str] = dict(visibility.dispatch_blocked_by) if visibility else {}

    # WHY a tool is withheld is a SEPARATE answer from THAT it is withheld, and
    # the pair composes into the category word a reader counts. `policy` beats
    # `mode` deliberately: when both apply, the policy is the answer that will
    # still be true after the user changes the mode.
    categories: Dict[str, str] = {}
    if visibility:
        for name in visibility.withheld_by_mode:
            categories.setdefault(name, "mode")
        for name in visibility.withheld_by_policy:
            categories[name] = "policy"
        for name in visibility.declared_internal:
            reasons.setdefault(
                name,
                "declared internal: executable by the harness, deliberately not advertised to the model",
            )
            categories.setdefault(name, "declared_internal")

    family_withheld, family_reason = _model_family_withheld(
        config, names, model=model, provider=provider
    )
    for name in family_withheld:
        reasons.setdefault(
            name,
            family_reason or "withheld by the declared model-family tool surface",
        )
        categories.setdefault(name, "model_family_surface")

    declared, deferred_source = deferred_names(config, searched=searched)
    if deferred:
        # An explicit argument is the caller stating a MEASURED deferral.
        extra = tuple(str(item).strip() for item in deferred if str(item).strip())
        if extra:
            declared = tuple(sorted(set(declared) | set(extra)))
            if deferred_source == "none":
                deferred_source = "search_event"
            elif "config" not in deferred_source:
                deferred_source = f"config+{deferred_source}"

    rows: List[CapabilityRow] = []
    for name in names:
        description = descriptions.get(name, "")
        if name in reasons:
            state = "withheld"
            reason_text = reasons[name]
        elif name in declared:
            state = "deferred"
            reason_text = (
                f"deferred by {deferred_source}" if deferred_source != "none" else "deferred"
            )
        else:
            state = "available"
            reason_text = ""
        rows.append(
            CapabilityRow(
                name=name,
                state=state,
                reason=reason_text,
                how=DEFERRED_HOW if state == "deferred" else "",
                description=description,
                category=categories.get(name, "deferred" if state == "deferred" else ""),
                dispatchable=False if name in blocked_by else None,
                dispatch_reason=blocked_by.get(name, ""),
                deferred_also=state == "withheld" and name in declared,
                # The mode surface and a deferral both describe the MODEL's
                # prompt. When they disagree about the same tool, the row is
                # marked rather than resolved — see `CapabilityReport.conflicts`.
                conflict=state == "deferred" and name in offered,
                conflict_detail=(
                    "the mode surface offers this tool to the model AND a deferral "
                    "says it is not in the prompt; deferred_tools is declared but "
                    "the surface authority does not implement it yet"
                    if (state == "deferred" and name in offered)
                    else ""
                ),
            )
        )
    # A tool named ONLY by the deferred set is not in the catalog, so it has no
    # row above. It is still a capability somebody told us about, and dropping
    # it is exactly the silent-absence defect. It gets its own row, marked.
    known = set(names)
    for name in declared:
        if name in known:
            continue
        rows.append(
            CapabilityRow(
                name=name,
                state="deferred",
                reason=f"deferred by {deferred_source}; not in this build's catalog",
                how=DEFERRED_HOW,
                description=descriptions.get(name, ""),
                category="deferred",
            )
        )

    counts = {state: 0 for state in TOOL_STATES}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    return CapabilityReport(
        rows=tuple(rows),
        mode=mode_name,
        catalog_total=len(rows),
        catalog_available=specs_available,
        catalog_reason=reason,
        deferred_source=deferred_source,
        counts=counts,
        offered=offered,
    )


def assert_no_false_capability(report: CapabilityReport) -> None:
    """Raise ``AssertionError`` if the report claims a missing capability it has.

    **This is the gate the whole phase exists for.** The harm is an INVERSION:
    the UI tells the user (and, through them, the agent) that a capability is
    absent when it is present, so a real answer becomes "switch models". An
    inversion is worse than an omission, so it fails loudly here rather than
    being left to a reader.
    """
    problems = report.false_capabilities()
    if problems:
        listing = "\n".join(
            f"  {item['kind']}: {item['tool']} - {item['detail']}" for item in problems
        )
        raise AssertionError(
            f"capability report claims {len(problems)} missing capability(ies):\n{listing}"
        )


def assert_descriptions_are_sanitised(report: CapabilityReport, lines: Sequence[str]) -> None:
    """Raise if a rendered line carries a description that skipped the sanitiser.

    Checked at the RENDER layer rather than the projection layer, because a
    correct projection and a renderer that prints it anyway is the failure this
    gate exists to catch. It compares against the RAW description text: the
    proof is that a hostile description cannot appear verbatim.
    """
    rendered = "\n".join(str(line) for line in lines)
    for row in report.rows:
        raw = str(row.description or "").strip()
        if len(raw) < 6 or "\n" in raw:
            continue
        if raw in rendered:
            raise AssertionError(
                f"{row.name!r}: its description reached the render unsanitised"
            )


# ---------------------------------------------------------------------------
# The renderers
# ---------------------------------------------------------------------------


def _bound(text: Any, limit: int) -> str:
    """Sanitise, then bound. The sanitiser is the one entry point."""
    from cli import ui

    value = ui.sanitize_text(text)
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def _line(text: Any, indent: str = "") -> str:
    """Sanitise, do NOT bound. For a line whose whole job is a disclosure.

    The repository records, as a PATTERN, four separate disclosures eaten by a
    width bound — the word "not" cut off a withheld reason is the same defect
    with a worse consequence, because the surviving half reads as permission.
    A soft-wrapped sentence still reads as a sentence.
    """
    from cli import ui

    return indent + " ".join(str(ui.sanitize_text(text)).split())


def capability_lines(report: CapabilityReport, *, width: int = 78) -> List[str]:
    """Render the capability view as PLAIN lines. Never empty, never raises.

    Shape, in reading order: the counts, then the withheld rows **with their
    reasons**, then the deferred rows **with the affordance**, then a bounded
    roster of what IS available. Withheld comes before deferred because a
    withheld tool needs a decision and a deferred one needs a habit.

    Every load-bearing sentence gets its own **unbounded** line. Only the
    available roster is bounded, because it is a list whose tail nobody is
    reading for a claim; a withheld reason is a claim.

    The three degenerate surfaces all render, and each says what it is:

    * no catalog -> "unavailable", with the reason, and an explicit statement
      that this is NOT a machine with no tools;
    * zero available -> "NONE", because a silent zero reads as a quiet shell;
    * zero withheld/deferred -> "none", which is a real zero because the
      counts were computed from a catalog that was actually read.
    """

    lines: List[str] = []
    if not report.catalog_available:
        lines.append("tool surface: unavailable")
        lines.append(_line(report.catalog_reason or "unknown", "  reason: "))
        lines.append(
            "  this is NOT a machine with no tools: the catalog could not be read"
        )
        return lines

    lines.append(f"tool surface: {report.counts_line()}")
    if report.mode:
        lines.append(_line(report.mode, "  mode: "))
    lines.append(_line(report.deferred_source, "  deferred set source: "))

    withheld = report.withheld
    if withheld:
        lines.append(f"  withheld ({len(withheld)}) - each one names its reason:")
        for row in withheld:
            lines.append(_line(row.name, "    - "))
            lines.append(_line(row.reason or "no reason recorded", "        why: "))
            if row.dispatchable is False:
                # The distinction that stops "withheld" reading as "broken":
                # hidden AND unrunnable is genuinely absent, and a reader who
                # cannot tell it apart from hidden-but-reachable goes looking
                # for a bug that is not there.
                lines.append(
                    _line(
                        row.dispatch_reason or "dispatch refused",
                        "        not reachable: ",
                    )
                )
            elif row.dispatchable:
                lines.append(
                    "        still dispatchable: hidden from the model, NOT unreachable"
                )
            if row.deferred_also:
                # Two authorities agreed, and the receipt says so rather than
                # dropping the second. The user has to know a search will NOT
                # get this one, because the mode is what is hiding it.
                lines.append(
                    "        also deferred: a search will NOT reach it while the reason above holds"
                )
    else:
        lines.append("  withheld: none")

    deferred = report.deferred
    if deferred:
        lines.append(f"  deferred ({len(deferred)}) - not in this prompt, discoverable:")
        for row in deferred:
            lines.append(_line(row.name, "    - "))
            lines.append(_line(row.reason, "        why: "))
            lines.append(_line(row.how, "        find it: "))
            if row.conflict:
                lines.append(_line(row.conflict_detail, "        CONFLICT: "))
    else:
        lines.append("  deferred: none")

    available = report.available
    if available:
        names = ", ".join(row.name for row in available)
        lines.append(f"  available ({len(available)}): {_bound(names, width - 16)}")
    else:
        # Zero available is a REAL and alarming state; it must not read as a
        # quiet machine with nothing to say.
        lines.append("  available: NONE - every tool is deferred or withheld")
    lines.append(
        "  a tool you expect here is not a broken tool: read its reason above"
    )
    conflicts = report.conflicts()
    if conflicts:
        lines.append(
            f"  {len(conflicts)} CONFLICT(S): two authorities disagree about the same "
            "tool, and this view does not pick a winner"
        )
    return lines


# ---------------------------------------------------------------------------
# The capability gap — the highest-value sentence in this phase
# ---------------------------------------------------------------------------


def inability_claim(text: Any) -> str:
    """Return the matched GAP marker in ``text``, or ``""``.

    A closed, small marker list on purpose. A broad refusal-shaped matcher
    would fire on ordinary prose ("I cannot see the diff yet") and the
    discoverability ledger would fill with rows nobody can act on — and a
    measurement T1 reads for the ablation is worse useless than absent.
    """
    body = str(text or "").lower()
    if not body:
        return ""
    for marker in GAP_MARKERS:
        if marker in body:
            return marker
    return ""


def _match_words(name: str, tool_keywords: Optional[Mapping[str, Iterable[str]]]) -> set:
    """The words that suggest ``name`` is what a claim is about.

    A tool name is split on ``_``/``-`` and the pieces of three or more
    characters count, because requiring the exact ``web_search`` in the claim
    is CIRCULAR: the affordance exists to teach a user that name, so requiring
    them to already have typed it means the hint can only fire for somebody who
    did not need it.

    The false-positive direction is deliberate. ``git_status`` deferred plus a
    claim mentioning "git" raises a hint that says a deferred tool **may** cover
    it, and a hedged hint costs one line. The false-negative direction costs the
    phase: the agent says it cannot do something it can, the user switches
    models, and nothing was wrong. Two-character fragments are excluded so
    ``mcp`` does not fire on every sentence containing "mc".
    """
    words = {str(name).lower()}
    for part in re.split(r"[^a-z0-9]+", str(name).lower()):
        if len(part) >= 3 and part not in _NAME_STOPWORDS:
            words.add(part)
    if tool_keywords:
        words.update(str(word).lower() for word in (tool_keywords.get(name) or ()))
    words.discard("")
    return words


def capability_gap_hint(
    text: Any,
    report: CapabilityReport,
    *,
    tool_keywords: Optional[Mapping[str, Iterable[str]]] = None,
) -> List[str]:
    """Return the gap hint when a deferred tool plausibly covers the claim.

    Returns ``[]`` — silence, not a hint — when the agent did not claim a
    limitation, or when nothing the agent has is DEFERRED. Two rules keep this
    from becoming noise:

    * only **deferred** tools trigger the hint. An **available** tool is in the
      prompt, so the agent already has it and telling the user to search for it
      would be advice that cannot help;
    * a **withheld** tool never triggers it, because the honest answer there is
      the withheld row's reason and pretending a search would find it is the
      lie this phase is about.

    ``tool_keywords`` maps a tool name to the words that suggest it. It is an
    INJECTION POINT rather than a guess: this module does not invent a
    vocabulary for 45 tools, and a caller that has one supplies it. See
    :func:`_match_words` for what happens with none.
    """
    marker = inability_claim(text)
    if not marker:
        return []
    if not report.deferred:
        return []
    body = str(text or "").lower()
    hits: List[CapabilityRow] = []
    for row in report.deferred:
        if any(word in body for word in _match_words(row.name, tool_keywords)):
            hits.append(row)
    if not hits:
        return []
    names = ", ".join(row.name for row in hits)
    return [
        f"CAPABILITY GAP: the agent said it could not do this ({marker!r}), "
        f"but {len(hits)} deferred tool(s) may cover it: {names}",
        f"  {CAPABILITY_GAP_HOW}",
        "  this is a discoverability miss and it has been recorded",
    ]


# ---------------------------------------------------------------------------
# The discoverability ledger — the measurement T1's ablation reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoverabilityMiss:
    """One recorded miss: the agent claimed a limit a deferred tool covered."""

    task_id: str
    marker: str
    tools: Tuple[str, ...]
    claim: str
    ts: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "marker": self.marker,
            "tools": list(self.tools),
            "claim": self.claim,
            "ts": float(self.ts),
        }


def _miss_path(log_root: Any, task_id: str = "") -> Optional[Path]:
    """Where misses are written, or ``None`` when there is nowhere to write."""
    try:
        root = Path(log_root)
    except (TypeError, ValueError):
        return None
    if str(task_id or "").strip():
        root = root / str(task_id).strip()
    return root / DISCOVERABILITY_FILENAME


def record_discoverability_miss(
    log_root: Any,
    miss: DiscoverabilityMiss,
    *,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Append one miss to the run's discoverability ledger.

    Returns ``(written, reason)``. **A miss that cannot be written is
    REPORTED, never counted as recorded** — a measurement that claims to exist
    and does not is how an ablation concludes a design works.

    The ledger is JSONL beside the run's journal, bounded by
    :data:`MAX_DISCOVERABILITY_MISSES` on read. It is written by append and
    read back by :func:`load_discoverability`, so a second process sees the
    same rows.
    """
    path = _miss_path(log_root, miss.task_id)
    if path is None:
        return False, "no log root: the miss was NOT recorded"
    row = miss.as_dict()
    if now is not None:
        row["ts"] = float(now)
    elif not row.get("ts"):
        row["ts"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:
        return False, f"miss NOT recorded: {type(exc).__name__}"
    return True, f"recorded in {path.parent.name}/{path.name}"


def load_discoverability(log_root: Any, task_id: str = "") -> Dict[str, Any]:
    """Read the ledger back as a measurement. Never raises.

    ``recorded: False`` with a reason when there is nothing to read — an absent
    ledger is **not** a zero-miss run, and T1's ablation needs the difference.
    """
    empty: Dict[str, Any] = {
        "available": False,
        "reason": "",
        "misses": 0,
        "tools": {},
        "markers": {},
        "rows": [],
        "truncated": False,
    }
    path = _miss_path(log_root, task_id)
    if path is None or not path.is_file():
        empty["reason"] = "no discoverability ledger for this run"
        return empty
    rows: List[Dict[str, Any]] = []
    tools: Dict[str, int] = {}
    markers: Dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        empty["reason"] = f"ledger unreadable: {type(exc).__name__}"
        return empty
    truncated = False
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        rows.append(row)
        markers[str(row.get("marker") or "")] = markers.get(str(row.get("marker") or ""), 0) + 1
        for tool in row.get("tools") or ():
            name = str(tool)
            tools[name] = tools.get(name, 0) + 1
    if len(rows) > MAX_DISCOVERABILITY_MISSES:
        rows = rows[-MAX_DISCOVERABILITY_MISSES:]
        truncated = True
    return {
        "available": True,
        "reason": "",
        "misses": len(rows),
        "tools": dict(sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))),
        "markers": dict(sorted(markers.items(), key=lambda kv: (-kv[1], kv[0]))),
        "rows": rows,
        "truncated": truncated,
    }


def discoverability_lines(report: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render the ledger as PLAIN lines. Non-empty even when there is no ledger."""
    lines = ["discoverability misses"]
    if not report.get("available"):
        lines.append(
            f"  none recorded: {_bound(report.get('reason') or 'no ledger', width - 20)}"
        )
        lines.append("  an absent ledger is NOT a run with zero misses")
        return lines
    misses = int(report.get("misses") or 0)
    lines.append(f"  {misses} miss(es) - the agent claimed a limit a deferred tool covered")
    tools = report.get("tools") or {}
    if tools:
        for name, count in list(tools.items())[:8]:
            lines.append(f"    - {name}: {count}")
    else:
        lines.append("    (no tool named: every miss was unattributable)")
    if report.get("truncated"):
        lines.append(
            f"  showing the last {MAX_DISCOVERABILITY_MISSES} rows; earlier rows were dropped"
        )
    return lines


# ---------------------------------------------------------------------------
# The tool-search event, and per-turn composition
# ---------------------------------------------------------------------------


def tool_search_projection(record: Any) -> Dict[str, Any]:
    """Project one ``tool_search`` journal row into a run fact.

    The four fields the brief asks for — what was searched, what was found,
    what it cost, and whether it found nothing — and the last one is a
    DISTINCT state rather than an empty list, because "the search found nothing"
    and "the search did not run" are different facts and only one of them is
    the agent's failure.
    """
    data: Mapping[str, Any] = {}
    if isinstance(record, Mapping):
        data = record.get("data") if isinstance(record.get("data"), Mapping) else record
    else:
        data = getattr(record, "data", {}) or {}
    queried = str(data.get("query") or data.get("queried") or data.get("search") or "")
    found_raw = data.get("found")
    found: Tuple[str, ...]
    if isinstance(found_raw, (list, tuple, set, frozenset)):
        found = tuple(str(item) for item in found_raw)
    elif found_raw in (None, ""):
        found = ()
    else:
        found = (str(found_raw),)
    ran = bool(data.get("ran", True))
    tokens = data.get("tokens")
    cost = data.get("cost_usd")
    return {
        "query": queried,
        "found": list(found),
        "ran": ran,
        "found_count": len(found),
        "empty": ran and not found,
        "tokens": tokens,
        "cost_usd": cost,
        "cost_known": cost is not None,
        "reason": str(data.get("reason") or ""),
    }


def tool_search_lines(facts: Sequence[Mapping[str, Any]], *, width: int = 78) -> List[str]:
    """Render every tool-search event in the run view. Non-empty when asked for."""
    lines = ["tool search"]
    if not facts:
        lines.append("  no tool search recorded - the agent never looked for a deferred tool")
        return lines
    for index, fact in enumerate(facts, start=1):
        query = str(fact.get("query") or "") or "(no query recorded)"
        lines.append(f"  {index}. searched: {_bound(query, width - 18)}")
        if not fact.get("ran"):
            lines.append("     did not run: " + _bound(str(fact.get("reason") or "no reason"), width - 20))
            continue
        if fact.get("empty"):
            lines.append(
                "     FOUND NOTHING - this capability may exist and was not offered"
            )
            continue
        names = ", ".join(str(item) for item in (fact.get("found") or ()))
        cost = fact.get("cost_usd")
        money = f"${float(cost):.6f}" if fact.get("cost_known") and cost is not None else "cost unknown"
        lines.append(f"     found {int(fact.get('found_count') or 0)}: {_bound(names, width - 20)}")
        lines.append(f"     cost: {money}")
    return lines


def turn_composition_projection(
    rows: Iterable[Any],
    *,
    names: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Per-turn tool composition, one row per turn that recorded one.

    This is the "per-turn composition is in the run view" requirement, and it is
    derived from what each turn's journal rows actually SAY the model was
    offered — never from the catalog, which is a per-run constant and would
    make every turn look identical.

    A turn with no recorded composition yields ``available: None`` with a
    reason. Reporting ``0`` there would say "this turn had no tools", which is
    a different claim from "this turn's tools were not recorded".
    """
    catalog = tuple(str(item) for item in (names or ()))
    per_turn: Dict[int, Dict[str, Any]] = {}
    for raw in rows or ():
        data: Mapping[str, Any] = {}
        if isinstance(raw, Mapping):
            data = raw.get("data") if isinstance(raw.get("data"), Mapping) else raw
        else:
            data = getattr(raw, "data", {}) or {}
        if not isinstance(data, Mapping):
            continue
        composition = data.get("tool_composition") or data.get("surface")
        if not isinstance(composition, Mapping):
            continue
        try:
            turn = int(data.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        available = composition.get("available") or composition.get("model_visible") or ()
        deferred = composition.get("deferred") or ()
        withheld = composition.get("withheld") or ()
        per_turn[turn] = {
            "turn": turn,
            "available": [str(item) for item in available],
            "deferred": [str(item) for item in deferred],
            "withheld": [str(item) for item in withheld],
            "known": bool(catalog) or bool(available),
        }
    return [per_turn[key] for key in sorted(per_turn)]


def turn_composition_lines(
    facts: Sequence[Mapping[str, Any]],
    *,
    width: int = 78,
) -> List[str]:
    """Render per-turn composition for the run view. Never empty when asked for."""
    lines = ["what the agent could reach, per turn"]
    if not facts:
        lines.append(
            "  no per-turn composition recorded - the run did not journal it"
        )
        lines.append("  that is NOT a turn with no tools")
        return lines
    for fact in facts:
        available = list(fact.get("available") or ())
        deferred = list(fact.get("deferred") or ())
        withheld = list(fact.get("withheld") or ())
        lines.append(
            f"  turn {int(fact.get('turn') or 0)}: {len(available)} available"
            + (f" · {len(deferred)} deferred" if deferred else "")
            + (f" · {len(withheld)} withheld" if withheld else "")
        )
        if deferred:
            lines.append(f"    deferred: {_bound(', '.join(deferred), width - 14)}")
        if withheld:
            lines.append(f"    withheld: {_bound(', '.join(withheld), width - 14)}")
    return lines


# ---------------------------------------------------------------------------
# A standalone probe, because the CLI needs a door and a receipt
# ---------------------------------------------------------------------------


def _cli(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - thin
    """``python -m cli.capability_view`` — print the view and its verdict."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m cli.capability_view")
    parser.add_argument("--mode", default="", help="approval mode to project")
    parser.add_argument("--json", action="store_true", help="print the receipt")
    # `argv or ()` would hand `parse_args` an explicit EMPTY list, which
    # argparse reads as "no arguments at all" and never falls back to
    # `sys.argv`. The flag would then be silently ignored and the receipt would
    # render the default surface for a mode the caller asked for — a receipt
    # describing a run that did not happen.
    args = parser.parse_args(None if argv is None else list(argv))
    report = tool_capability_report(mode=args.mode or None)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        for line in capability_lines(report):
            print(line)
    problems = report.false_capabilities()
    if problems:
        for item in problems:
            print(f"  FALSE CAPABILITY: {item['kind']}: {item['tool']} - {item['detail']}")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
