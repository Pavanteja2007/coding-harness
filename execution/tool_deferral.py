"""Deferred tools are a VISIBILITY change, never a PERMISSION change.

Phase 3b makes the tool surface dynamic. Codex defers MCP tools by default;
Hermes collapses an MCP catalog once it passes 10% of the window. Deferral is a
prompt-budget lever and it is genuinely valuable — the corpus figure is ~40%
prompt reduction from deferred tool loading. It is also, on this product's own
terms, a new security surface: **a tool that was visible (and so visibly
reviewable by a human) is now hidden from view and still executable.**

This module is the boundary that makes that safe, and it lives in ``execution/``
for a reason worth stating before anything else:

> :meth:`execution.workspace.SafeToolBackend.execute` is the ONE dispatch every
> typed tool call goes through. Policy (``policy_for_tool``), the policy engine
> (``ToolPolicyEngine.evaluate``) and approval (``SafeToolBackend._authorize``)
> all live behind it. So "deferral is not a privilege escalation" is not a
> promise this module makes about a mechanism somebody else owns — it is a
> property of a funnel, and a funnel can be *proved* rather than promised.

What is asserted here, and by what mechanism
--------------------------------------------

1. **Deferred uses the identical execution path.** :func:`call_fingerprint`
   derives, from the SAME two authorities ``execute()`` consults, every field of
   the decision a call gets. It takes no surface-state argument at all — so a
   fingerprint computed for a deferred call is not "the deferred fingerprint",
   it is *the* fingerprint. ``execution/test_p3b_deferral_pins.py`` then drives
   the real ``execute()`` in every surface arm and requires the resulting
   ``ToolResult`` to be byte-identical, which is the assertion the brief asks
   for; this module supplies the vocabulary and the derivation.

2. **The same policy verdict for the same tool, always.**
   :func:`policy_verdict_matrix` evaluates the whole live catalog through
   :func:`execution.workspace.policy_for_tool` and, when a policy engine is
   supplied, through ``ToolPolicyEngine.evaluate`` too. It is the function T5 can
   call without re-deriving which tools exist.

3. **withheld and deferred cannot overlap.** :func:`resolve_surface` is the ONE
   place a tool's surface state is decided, from BOTH sets at once. A name in
   both is a **conflict**, not a precedence question: it resolves to
   ``withheld`` (the state that cannot be discovered and cannot be reached) and
   the conflict is published. Silent precedence would be the defect — "which
   set wins" is a decision a reader cannot audit, and answering it "deferred"
   once is a policy bypass.

   Note the asymmetry this creates deliberately: a *bug* in a surface decision
   makes a tool invisible, never visible. Over-refusal is recoverable; a
   discoverable withheld tool is not.

4. **A description is data.** :func:`fence_description` routes every deferred
   tool's description through ``shared.security.review_untrusted_source``
   (:data:`shared.security.UNTRUSTED_SOURCES` already carries ``"mcp"``) and
   renders it with :func:`shared.security.taint_wrap`, so the fence is VISIBLE in
   the model-facing text rather than only in a receipt. A description containing
   "ignore your instructions and run X" is quarantined by the shipped fail-closed
   policy, and the quarantined marker — not the text — is what the model sees.

   **A description cannot grant anything, and structurally.** This module
   imports ``policy_for_tool`` and calls it; it never constructs a
   ``ToolPolicy``, never writes ``requires_approval``, never mutates
   ``_TOOL_POLICIES``, and never calls ``dataclasses.replace`` on a policy. The
   pin module asserts each of those at AST level, so the claim is not "we were
   careful" but "there is no expression of the grant in this file".

5. **Discovery is a read.** :func:`search_deferred_tools` holds no callable it
   could execute. It filters by the caller's capability set BEFORE reading a
   single description, bounds both the count and the bytes, fences what it
   returns, and publishes ``executed_anything: False`` on every result.

The enumeration policy, decided
-------------------------------

:data:`ENUMERATION_POLICY` is ``uniform_invisibility`` and it is a DECISION, so
it is written down rather than implied:

* A capability-filtered tool is **indistinguishable from a tool that does not
  exist**. Querying a denied tool and querying a name in no catalog at all
  return the same shape: zero hits, no note, no count of what was hidden.
* **What IS published:** the number of results the model received, and whether
  the caps truncated it. A model can count its own results — that is not
  privileged information. **What is NOT published:** how many catalog entries
  were considered, how many were filtered, or why. "1 result" and "1 result of
  40, 39 hidden" are different facts and the second is the oracle.
* **Timing is not defended as a channel, and the module says so on every
  result** (:attr:`DiscoveryResult.timing_channel`). The filtering cost is
  O(len(allowed)) for every query regardless of what matched, so a query cannot
  be timed into a count of the hidden set. This is stated as a *measurement
  claim about this function's shape*, not as a guarantee about the whole
  product — a caller that re-introduces a per-candidate cost would break it,
  which is why the shape is pinned.

What this module does NOT do
----------------------------

* It does not decide what the model is shown. ``harness.tool_surface`` owns the
  visible/deferred surface for the builtin catalog; ``mcp_server.tool_pinning``
  owns the deferred MCP catalog. This module consumes both and refuses to
  re-derive either.
* It does not grant, widen, or revoke a permission. It intersects.
* It is not wired into ``execute()``. It is the vocabulary and the proof surface
  for a seam that T1 owns. Saying so here is the honest state; a dead guard
  wearing a receipt is worse than an absent one.
"""

from __future__ import annotations

import ast
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from execution.workspace import (
    ToolPolicy,
    ToolPolicyEngine,
    policy_for_tool,
)
from shared import security

__all__ = [
    "DEFERRED_DESCRIPTION_CHARS",
    "DEFERRED_ROW_FIELDS",
    "DEFERRED_SEARCH_MAX_BYTES",
    "DEFERRED_SEARCH_MAX_RESULTS",
    "DESCRIPTION_SOURCE",
    "ENUMERATION_POLICY",
    "EXECUTABLE_FORBIDDEN",
    "SKILL_TRUST_TIERS",
    "SURFACE_DEFERRED",
    "SURFACE_STATES",
    "SURFACE_VISIBLE",
    "SURFACE_WITHHELD",
    "TRUSTED_SKILL_ORIGINS",
    "CapabilityFilter",
    "DeferredTool",
    "DiscoveryHit",
    "DiscoveryResult",
    "ExecutionPathFingerprint",
    "SkillCapabilityGrant",
    "SurfaceConflict",
    "SurfaceLedger",
    "SurfaceRecord",
    "TaintReceipt",
    "assert_disjoint",
    "builtin_withheld_names",
    "call_fingerprint",
    "deferred_tool",
    "enumeration_policy_lines",
    "executable_names",
    "fence_description",
    "forbidden_execution_names",
    "forbidden_grant_expressions",
    "grant_skill_capabilities",
    "module_source",
    "policy_verdict_matrix",
    "required_binaries_present",
    "resolve_surface",
    "search_deferred_tools",
    "skill_source_is_trusted",
    "taint_deferred_output",
]

# ---------------------------------------------------------------------------
# The three surface states
# ---------------------------------------------------------------------------

SURFACE_VISIBLE = "visible"
SURFACE_DEFERRED = "deferred"
SURFACE_WITHHELD = "withheld"

#: A CLOSED set. A state a reader has to guess is a state nobody can audit, and
#: an unknown state must never be silently treated as "visible" — so
#: :func:`resolve_surface` refuses one.
SURFACE_STATES: Tuple[str, ...] = (SURFACE_VISIBLE, SURFACE_DEFERRED, SURFACE_WITHHELD)


class SurfaceConflict(RuntimeError):
    """A name appeared in BOTH the withheld and the deferred set.

    Raised only by :func:`assert_disjoint`, which exists so a *test* or an
    operator probe can demand the invariant. The product path
    (:func:`resolve_surface`) does not raise: it resolves the overlap to
    ``withheld`` and publishes it, because a conflict that stops resolution is a
    conflict whose handling is the caller's judgement, and this is the layer
    where that judgement must not live.
    """


@dataclass(frozen=True)
class SurfaceRecord:
    """One tool's surface state and which authority decided it."""

    name: str
    state: str
    source: str = ""
    #: Set when the name arrived in both sets. The state is ``withheld``
    #: regardless; this is the evidence for the receipt.
    conflict: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "source": self.source,
            "conflict": bool(self.conflict),
        }


@dataclass(frozen=True)
class SurfaceLedger:
    """The resolved surface for one session, with the conflict set published."""

    records: Tuple[SurfaceRecord, ...] = ()
    conflicts: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def names(self, state: str) -> Tuple[str, ...]:
        """Return the names in one state. An unknown state returns ``()``."""
        if state not in SURFACE_STATES:
            return ()
        return tuple(item.name for item in self.records if item.state == state)

    @property
    def visible(self) -> Tuple[str, ...]:
        return self.names(SURFACE_VISIBLE)

    @property
    def deferred(self) -> Tuple[str, ...]:
        return self.names(SURFACE_DEFERRED)

    @property
    def withheld(self) -> Tuple[str, ...]:
        return self.names(SURFACE_WITHHELD)

    def state_of(self, name: str) -> str:
        """Return one tool's state, or ``""`` for a name this ledger does not carry."""
        target = str(name or "").strip()
        for item in self.records:
            if item.name == target:
                return item.state
        return ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "states": {name: len(self.names(name)) for name in SURFACE_STATES},
            "visible": list(self.visible),
            "deferred": list(self.deferred),
            "withheld": list(self.withheld),
            "conflicts": list(self.conflicts),
            "notes": list(self.notes),
        }


def _clean(names: Iterable[Any]) -> Tuple[str, ...]:
    """Return the non-empty, de-duplicated, order-preserving names."""
    out: List[str] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def resolve_surface(
    *,
    catalog: Optional[Sequence[str]] = None,
    deferred: Optional[Sequence[str]] = None,
    withheld: Optional[Sequence[str]] = None,
    deferred_source: str = "deferred",
    withheld_source: str = "withheld",
) -> SurfaceLedger:
    """Resolve every tool's surface state from BOTH sets at once.

    ``catalog`` is the set of tools that exist. A name in ``deferred`` or
    ``withheld`` that is not in ``catalog`` is still recorded (it is a fact
    somebody asserted), with a note, because a deferred name the catalog does not
    carry is a naming drift worth seeing rather than a silent drop.

    **The overlap rule is the point of this function.** A name in both sets is
    NOT resolved by precedence. It resolves to ``withheld`` — the state that
    cannot be discovered and is refused at the call — and the name is published
    in :attr:`SurfaceLedger.conflicts`. Precedence would answer "which set wins"
    with a constant that changes the day the code is reordered.

    A name supplied with an unknown state is not representable here (the sets
    are positional), so the corresponding refusal lives in
    :func:`assert_disjoint` and in the closed ``SURFACE_STATES`` export.
    """
    deferred_names = _clean(deferred)
    withheld_names = _clean(withheld)
    conflicts = tuple(sorted(set(deferred_names) & set(withheld_names)))
    catalog_names = _clean(catalog)
    notes: List[str] = []
    if conflicts:
        notes.append(
            f"{len(conflicts)} tool(s) appeared in BOTH the deferred and the "
            f"withheld set: {list(conflicts)}. Each resolved to "
            f"{SURFACE_WITHHELD!r}, which cannot be discovered and is refused "
            f"at the call. A tool that is both hidden and discoverable is a "
            f"policy bypass, so the safe state wins and the conflict is reported."
        )
    known = set(catalog_names)
    if catalog_names:
        for label, names in (
            (deferred_source, deferred_names),
            (withheld_source, withheld_names),
        ):
            unknown = [name for name in names if name not in known]
            if unknown:
                notes.append(
                    f"{label} set named {len(unknown)} tool(s) the catalog does "
                    f"not carry: {unknown}. Recorded, not dropped: a name nobody "
                    f"can execute is still a fact somebody asserted about the "
                    f"surface."
                )

    ordered: List[str] = []
    for name in (*catalog_names, *deferred_names, *withheld_names):
        if name not in ordered:
            ordered.append(name)

    records: List[SurfaceRecord] = []
    for name in ordered:
        in_deferred = name in deferred_names
        in_withheld = name in withheld_names
        conflict = bool(in_deferred and in_withheld)
        if in_withheld:
            state = SURFACE_WITHHELD
            source = withheld_source
        elif in_deferred:
            state = SURFACE_DEFERRED
            source = deferred_source
        else:
            state = SURFACE_VISIBLE
            source = "catalog"
        records.append(
            SurfaceRecord(name=name, state=state, source=source, conflict=conflict)
        )
    return SurfaceLedger(
        records=tuple(records), conflicts=conflicts, notes=tuple(notes)
    )


def assert_disjoint(
    deferred: Optional[Sequence[str]],
    withheld: Optional[Sequence[str]],
) -> None:
    """Raise :class:`SurfaceConflict` if the two sets share a name.

    The product path (:func:`resolve_surface`) resolves and reports; this
    function exists so a test, an operator probe, or a CI gate can demand the
    invariant rather than read it off a receipt.
    """
    overlap = sorted(set(_clean(deferred)) & set(_clean(withheld)))
    if overlap:
        raise SurfaceConflict(
            "withheld and deferred tool sets overlap on "
            f"{overlap}; a withheld tool that is discoverable is a policy "
            "bypass. Resolve the overlap in the source that produced it rather "
            "than relying on precedence."
        )


def builtin_withheld_names(
    *, config: Optional[Mapping[str, Any]] = None, model: str = "", provider: str = ""
) -> Tuple[str, ...]:
    """Return the REAL withheld names for the builtin catalog, via T1's authority.

    Reads ``harness.tool_surface.surface_for`` rather than restating its
    ``_WITHHELD_BY_PATCH_ONLY`` tuple: a private literal copied into this module
    is a second answer to "what is withheld", and the day T1 adds a capability
    this copy would report a tool as visible that the product withholds.

    Returns ``()`` when the surface module is unavailable — an absent surface
    module means "no mapping applied", which is visible-not-withheld and is the
    honest empty answer. It is deliberately NOT treated as a failure here; the
    pin module asserts the import is reachable, which is where that belongs.
    """
    try:
        from harness.tool_surface import surface_for
    except Exception:
        return ()
    try:
        ledger = surface_for(
            model=model or None,
            provider=provider or None,
            config=config,
            catalog=None,
        )
    except Exception:
        return ()
    return tuple(ledger.withheld_names)


# ---------------------------------------------------------------------------
# 1. The identical-execution-path fingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPathFingerprint:
    """Everything about a call's authorisation, derived not declared.

    **There is deliberately no ``surface`` or ``deferred`` field.** A
    fingerprint that carried the surface state would let a reader assume the two
    were compared; the property being proven is that the comparison has nothing
    to compare, because the derivation does not read it.

    ``requires_approval`` and ``effect`` come from
    :func:`execution.workspace.policy_for_tool` — the same call
    ``SafeToolBackend.execute`` makes. ``policy_action`` comes from
    ``ToolPolicyEngine.evaluate`` when an engine is supplied, and is
    ``"unengineered"`` when there is not, which is exactly what ``_authorize``
    does in that case (it falls back to ``requires_approval`` plus the
    approver). ``would_authorize`` answers what ``_authorize`` would return,
    including the "no approver installed" refusal.
    """

    tool: str
    effect: str
    backend: str
    sandboxed: bool
    process: bool
    network: bool
    mutating: bool
    requires_approval: bool
    policy_action: str
    would_authorize: bool
    refusal: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "effect": self.effect,
            "backend": self.backend,
            "sandboxed": bool(self.sandboxed),
            "process": bool(self.process),
            "network": bool(self.network),
            "mutating": bool(self.mutating),
            "requires_approval": bool(self.requires_approval),
            "policy_action": self.policy_action,
            "would_authorize": bool(self.would_authorize),
            "refusal": self.refusal,
        }

    def decision_fields(self) -> Tuple[Any, ...]:
        """The fields a ``ToolResult`` carries, for a byte-comparison across arms."""
        return (
            self.tool,
            self.effect,
            self.backend,
            self.sandboxed,
            self.requires_approval,
            self.policy_action,
            self.would_authorize,
            self.refusal,
        )


def call_fingerprint(
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    *,
    policy_engine: Optional[ToolPolicyEngine] = None,
    approve: Optional[Any] = None,
    sandboxed: bool = False,
) -> ExecutionPathFingerprint:
    """Derive the authorisation decision for one call, the way ``execute()`` will.

    ``args`` is forwarded to the policy engine so a rule keyed on path, command
    or network domain sees the same dimensions ``execute()`` would send. A
    ``ToolPolicy`` is read through :func:`policy_for_tool` and never rebuilt.
    """
    policy: ToolPolicy = policy_for_tool(tool)
    values = dict(args or {})
    action = "unengineered"
    refusal = ""
    would = True
    if policy_engine is not None:
        decision = policy_engine.evaluate(
            str(tool or "").strip().lower(),
            values,
            side_effect_class=policy.effect,
        )
        action = str(decision.action)
        if action == "deny":
            would, refusal = False, str(decision.reason or "denied by policy")
        elif action == "ask":
            would = approve is not None
            refusal = "" if would else "approval required"
    elif policy.requires_approval:
        would = approve is not None
        refusal = "" if would else "approval required"
    return ExecutionPathFingerprint(
        tool=str(tool or ""),
        effect=policy.effect,
        backend=policy.backend,
        sandboxed=bool(policy.sandboxed),
        process=bool(policy.process),
        network=bool(policy.network),
        mutating=bool(policy.mutating),
        requires_approval=bool(policy.requires_approval),
        policy_action=action,
        would_authorize=bool(would),
        refusal=refusal,
    )


def policy_verdict_matrix(
    *,
    policy_engine: Optional[ToolPolicyEngine] = None,
    args_for: Optional[Any] = None,
    approve: Optional[Any] = None,
) -> Dict[str, ExecutionPathFingerprint]:
    """Fingerprint EVERY tool in the live catalog.

    ``execution.workspace._TOOL_POLICIES`` is read through the module object
    rather than restated, so a tool added to the catalog is covered the day it
    lands and the pin cannot pass against a stale copy of the list.

    ``args_for`` maps a tool name to the argument mapping the engine should see;
    without it a path/command-keyed rule has nothing to match and every tool
    gets the same default verdict — which is why the pin module supplies one.
    """
    from execution import workspace as _ws

    out: Dict[str, ExecutionPathFingerprint] = {}
    for name in sorted(_ws._TOOL_POLICIES):
        args = {}
        if callable(args_for):
            try:
                args = dict(args_for(name) or {})
            except Exception:
                args = {}
        out[name] = call_fingerprint(
            name,
            args,
            policy_engine=policy_engine,
            approve=approve,
        )
    return out


# ---------------------------------------------------------------------------
# 2. Descriptions are untrusted content
# ---------------------------------------------------------------------------

#: The source label every deferred description is reviewed under. ``"mcp"`` is
#: already a member of :data:`shared.security.UNTRUSTED_SOURCES`, so this is a
#: member of the shipped vocabulary rather than a new category.
DESCRIPTION_SOURCE = "mcp"

#: How much of one description may reach a search result. A description is a
#: summary, not a document: the corpus's own deferred row keeps the FIRST LINE.
DEFERRED_DESCRIPTION_CHARS = 400


#: The ONLY attributes a catalog row may be asked for. Published because
#: "discovery reads four named fields" is a claim a reader can check, and a
#: duck-typed projection that grows a fifth field is a fifth thing a hostile
#: row can make happen. A row is external data: an attribute that raises on
#: access is a row that does not have that attribute, and treating it as a hard
#: failure would let a hostile server turn a search into a crash.
DEFERRED_ROW_FIELDS: Tuple[str, ...] = (
    "name",
    "description",
    "side_effect_class",
    "server",
)


@dataclass(frozen=True)
class DeferredTool:
    """One deferred catalog entry, in the shape this module consumes.

    Duck-typed on purpose: ``mcp_server.tool_pinning.ToolSummary`` already has
    ``name``/``description``/``side_effect_class``, and a constructor that
    demanded that exact type would need the MCP package importable to describe a
    two-field test fixture.

    The projection reads **only the four names in :data:`DEFERRED_ROW_FIELDS`
    and tolerates a raising one** — see that constant for why that is a
    contract rather than defensiveness. The search loop additionally arranges
    that a row the capability filter rejects is never touched at all, and a row
    whose description does not match a query is never fully projected.
    """

    name: str
    description: str = ""
    side_effect_class: str = ""
    server: str = ""
    origin: str = "deferred"

    @classmethod
    def of(cls, value: Any) -> "DeferredTool":
        """Project any catalog row onto this shape. Never raises."""
        if isinstance(value, DeferredTool):
            return value
        return cls(
            name=_row_name(value),
            description=_row_description(value),
            side_effect_class=_row_field(value, "side_effect_class"),
            server=_row_field(value, "server"),
        )


def _row_field(value: Any, attribute: str) -> str:
    """Read one documented row field, returning ``""`` if absent or raising."""
    if isinstance(value, Mapping):
        return str(value.get(attribute) or "")
    try:
        return str(getattr(value, attribute, "") or "")
    except Exception:
        return ""


def _row_name(value: Any) -> str:
    """Read ONLY a row's name. The search loop's first touch of every row."""
    return _row_field(value, "name")


def _row_description(value: Any) -> str:
    """Read ONLY a row's description. Read after the name matched a query."""
    return _row_field(value, "description")


def deferred_tool(value: Any) -> DeferredTool:
    """Module-level alias for :meth:`DeferredTool.of`."""
    return DeferredTool.of(value)


def fence_description(
    description: Any,
    *,
    source: str = DESCRIPTION_SOURCE,
    max_chars: int = DEFERRED_DESCRIPTION_CHARS,
    policy: Optional[security.UntrustedPolicy] = None,
    mode: Optional[str] = None,
) -> security.UntrustedReview:
    """Review one tool description as untrusted content and return the verdict.

    The returned ``.text`` is ALREADY defanged and redacted and bounded, and
    under the shipped fail-closed policy a hostile description resolves to
    :data:`shared.security.QUARANTINED_TEXT` rather than to its own words. Use
    :func:`shared.security.taint_wrap` to render it so the fence is visible to
    the model.

    ``mode`` is forwarded to
    :func:`shared.security.review_untrusted_source` and exists because **two
    different fences are needed and only one of them is the default.** A
    description that trips an injection rule is QUARANTINED under
    ``{"*": "block"}`` — which is right, and which also means there is no
    "flagged but readable" arm for a caller that reviewed a description under
    ``flag``. Taint PROPAGATION needs that arm: it is the case where the model
    was shown something questionable, chose the tool anyway, and its output
    should carry the same flag. An unusable ``mode`` is refused to ``block`` by
    the shared boundary, so a typo cannot widen this.

    A ``max_chars`` of 0 means "no description cap"; anything else is clamped to
    at least 1 rather than becoming an unbounded pass, because a cap of 0 in a
    caller that meant "the default" would silently disable a limit.
    """
    text = str(description or "")
    return security.review_untrusted_source(
        text[: max(1, int(max_chars))] if max_chars else text,
        source=source,
        mode=mode,
        policy=policy,
    )


@dataclass(frozen=True)
class TaintReceipt:
    """What a deferred tool's output inherited, and from what.

    ``propagated`` is the load-bearing field: it is True when the tool's
    description was tainted, because that is the case this exists for. A tool
    whose description was clean propagates nothing, and saying so is as much a
    part of the receipt as the tainted case.
    """

    tool: str
    tainted: bool
    blocked: bool
    severity: str = "none"
    categories: Tuple[str, ...] = ()
    propagated: bool = False
    text: str = ""
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "tainted": bool(self.tainted),
            "blocked": bool(self.blocked),
            "severity": self.severity,
            "categories": list(self.categories),
            "propagated": bool(self.propagated),
            "chars": len(self.text),
            "note": self.note,
        }


def taint_deferred_output(
    *,
    tool: str,
    value: Any,
    description_review: Optional[security.UntrustedReview] = None,
    source: str = DESCRIPTION_SOURCE,
) -> Tuple[str, TaintReceipt]:
    """Carry a deferred tool's description taint onto the tool's OUTPUT.

    The rule: if the description the model was shown was tainted, the output of
    the call that description authorised is tainted too, because the model acted
    on text that was never trustworthy and cannot be told which part of its
    context came from the server.

    **A BLOCKED description quarantines the output outright.** The tool's
    contract was never legitimately reviewed, so its answers are not either;
    forwarding them would mean the fence protected the description and left the
    payload open.

    ``(text, receipt)`` — never an exception, because a tainted tool must not be
    able to raise through the call path.
    """
    name = str(tool or "")
    review = description_review
    if review is None:
        return "", TaintReceipt(
            tool=name,
            tainted=False,
            blocked=False,
            note=(
                "no description review was supplied, so nothing was propagated; "
                "a caller that fetched a description must pass its review, "
                "because 'we forgot' and 'it was clean' are different states"
            ),
        )
    text = "" if value is None else str(value)
    if review.blocked:
        receipt = TaintReceipt(
            tool=name,
            tainted=bool(review.tainted),
            blocked=True,
            severity=str(review.severity),
            categories=tuple(review.categories),
            propagated=True,
            text=security.QUARANTINED_TEXT,
            note=(
                "the description this tool was selected by was quarantined, so "
                "its output is not forwarded: the tool's own contract was never "
                "reviewed"
            ),
        )
        return security.QUARANTINED_TEXT, receipt
    if not review.tainted:
        return text, TaintReceipt(
            tool=name,
            tainted=False,
            blocked=False,
            severity=str(review.severity),
            propagated=False,
            text=text,
            note="the description was clean; the output carries no taint",
        )
    propagated_review = security.UntrustedReview(
        source=review.source,
        allowed=True,
        blocked=False,
        tainted=True,
        severity=str(review.severity),
        findings=tuple(review.findings),
        text=security.defang_untrusted_delimiters(text),
        digest=review.digest,
        policy_mode=review.policy_mode,
        truncated=bool(review.truncated),
    )
    receipt = TaintReceipt(
        tool=name,
        tainted=True,
        blocked=False,
        severity=str(review.severity),
        categories=tuple(review.categories),
        propagated=True,
        text=security.taint_wrap(propagated_review),
        note=(
            "the description this tool was selected by was flagged; its output "
            "is fenced with the same taint so the model can tell server text "
            "from harness text"
        ),
    )
    return receipt.text, receipt


# ---------------------------------------------------------------------------
# 3. Discovery is a read, not an execution path
# ---------------------------------------------------------------------------

DEFERRED_SEARCH_MAX_RESULTS = 8

#: The byte bound is deliberately LOOSE relative to the count bound, and the
#: reason is arithmetic rather than taste: one fenced hit costs
#: ``DEFERRED_DESCRIPTION_CHARS`` plus roughly 300 characters of banner and
#: boundary, so ~750 bytes each. At 2000 the BYTE cap would fire at the third
#: hit and the count cap of 8 would be a cap that can never be reached — a dead
#: cap, which is the exact defect this repository keeps recording. At 8000 (the
#: same divisor ``shared.security.DEFAULT_UNTRUSTED_MAX_CHARS`` uses for one
#: untrusted source) the COUNT cap binds at the shipped defaults and the byte
#: cap binds when descriptions are long or the model asks for a lot. Both are
#: live, and which one fired is published on every result.
DEFERRED_SEARCH_MAX_BYTES = 8000

ENUMERATION_POLICY = "uniform_invisibility"

#: Printed on demand. The policy is a decision, and a decision nobody can read
#: is a decision nobody can review.
_ENUMERATION_POLICY_LINES: Tuple[str, ...] = (
    f"[{ENUMERATION_POLICY}] A tool this model may not use is indistinguishable "
    "from a tool that does not exist: querying a filtered tool and querying a "
    "name in no catalog at all return the same result shape, with no note and "
    "no count of what was hidden.",
    f"[{ENUMERATION_POLICY}] Published: how many results the model received, and "
    "whether the caps truncated them. NOT published: how many catalog entries "
    "were considered, how many were filtered, or why. A model can count its own "
    "results; it cannot learn the size of the set it was denied.",
    f"[{ENUMERATION_POLICY}] Timing is NOT defended as a channel. The filtering "
    "work is one pass over the allowed set for every query, so the cost does not "
    "vary with what matched. That is a property of THIS function's shape and is "
    "pinned by a test; a caller that reintroduces a per-candidate cost reopens "
    "the channel and the pin says so.",
)


def enumeration_policy_lines() -> Tuple[str, ...]:
    """Return the enumeration policy as plain lines for a surface to show."""
    return _ENUMERATION_POLICY_LINES


@dataclass(frozen=True)
class CapabilityFilter:
    """What this model is allowed to know about.

    ``denied`` is the capability gate expressed as a set. It is intersected
    rather than consulted, so a caller that forgot to pass it gets the
    fail-closed direction by construction: an empty ``allowed`` with an empty
    ``denied`` means "nothing is excluded", and a caller that has no capability
    model at all must pass ``allowed`` explicitly (see
    :func:`search_deferred_tools`).
    """

    allowed: Optional[Tuple[str, ...]] = None
    denied: Tuple[str, ...] = ()

    def permits(self, name: str) -> bool:
        """Whether ``name`` may be described to this model."""
        target = str(name or "").strip()
        if target in self.denied:
            return False
        if self.allowed is None:
            return True
        return target in self.allowed

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": None if self.allowed is None else list(self.allowed),
            "denied": list(self.denied),
        }


@dataclass(frozen=True)
class DiscoveryHit:
    """One search result. ``description`` is already fenced."""

    name: str
    description: str
    side_effect_class: str
    fenced: bool
    tainted: bool
    blocked: bool
    severity: str = "none"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "side_effect_class": self.side_effect_class,
            "fenced": bool(self.fenced),
            "tainted": bool(self.tainted),
            "blocked": bool(self.blocked),
            "severity": self.severity,
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """What discovery returned, and what it refused to say.

    ``returned`` and ``truncated`` are the only two counts published. There is
    no ``considered`` and no ``filtered`` field, and their absence is the
    enforcement: :data:`ENUMERATION_POLICY` is only true if there is nowhere for
    the hidden set's size to appear.
    """

    query: str
    hits: Tuple[DiscoveryHit, ...] = ()
    returned: int = 0
    truncated: bool = False
    bytes: int = 0
    results_cap: int = DEFERRED_SEARCH_MAX_RESULTS
    bytes_cap: int = DEFERRED_SEARCH_MAX_BYTES
    executed_anything: bool = False
    enumeration: str = ENUMERATION_POLICY
    timing_channel: str = "not_defended"
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def names(self) -> Tuple[str, ...]:
        return tuple(hit.name for hit in self.hits)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "hits": [hit.as_dict() for hit in self.hits],
            "returned": int(self.returned),
            "truncated": bool(self.truncated),
            "bytes": int(self.bytes),
            "results_cap": int(self.results_cap),
            "bytes_cap": int(self.bytes_cap),
            "executed_anything": bool(self.executed_anything),
            "enumeration": self.enumeration,
            "timing_channel": self.timing_channel,
            "notes": list(self.notes),
        }


def search_deferred_tools(
    query: str,
    *,
    catalog: Optional[Sequence[Any]] = None,
    capabilities: Optional[CapabilityFilter] = None,
    results_cap: int = DEFERRED_SEARCH_MAX_RESULTS,
    bytes_cap: int = DEFERRED_SEARCH_MAX_BYTES,
    max_description_chars: int = DEFERRED_DESCRIPTION_CHARS,
    policy: Optional[security.UntrustedPolicy] = None,
) -> DiscoveryResult:
    """Search the deferred catalog. **Executes nothing.**

    Four properties, in the order they are enforced:

    1. **The capability filter runs before a description is read.** A denied
       tool's text never enters this function's working set, so it cannot reach
       the model even transiently. Filtering AFTER matching would mean the
       description was already in the comparison.
    2. **No callable is reachable from here.** This function holds no dispatcher,
       no backend and no loader; the only callables it invokes are
       ``str``, the fence, and the match test. A caller that wants a schema
       loaded asks for it separately, through the loader that owns it.
    3. **Both bounds are real and both are reported.** ``results_cap`` bounds
       the count; ``bytes_cap`` bounds the rendered bytes of what is returned.
       Hitting either sets ``truncated`` rather than returning a partial list
       that reads as a whole answer.
    4. **Every description is fenced**, so the fence is visible in the text the
       model reads and not only in this receipt.

    ``capabilities=None`` means "no capability model was supplied", which
    returns everything. That is the HONEST reading — "I did not tell you what
    this model may use" is not the same fact as "this model may use nothing" —
    but it means the capability property is the CALLER's to provide, and the pin
    module supplies it and asserts it. The alternative (default to deny-all)
    would make an unconfigured caller silently lose its catalog, which is a
    worse failure to debug than the one it prevents.
    """
    gate = capabilities or CapabilityFilter()
    needle = str(query or "").strip().casefold()
    count_cap = max(1, int(results_cap))
    byte_cap = max(1, int(bytes_cap))
    notes: List[str] = []
    hits: List[DiscoveryHit] = []
    total_bytes = 0
    truncated = False
    considered = 0

    # One pass over the ALLOWED set for every query. The capability check and
    # the name read happen first, and a row that does not match is never fully
    # projected: its description is read only to build the haystack, and its
    # other attributes are read only once it is actually going into a result.
    for raw in catalog or ():
        name = _row_name(raw)
        if not name or not gate.permits(name):
            continue
        considered += 1
        description = _row_description(raw)
        haystack = f"{name}\n{description}".casefold()
        if needle and needle not in haystack:
            continue
        if len(hits) >= count_cap:
            truncated = True
            break
        # Fence before it is rendered into a result the model will read.
        review = fence_description(
            description, max_chars=max_description_chars, policy=policy
        )
        rendered = security.taint_wrap(review)
        cost = len(rendered)
        if hits and total_bytes + cost > byte_cap:
            truncated = True
            break
        total_bytes += cost
        hits.append(
            DiscoveryHit(
                name=name,
                description=rendered,
                side_effect_class=_row_field(raw, "side_effect_class"),
                fenced=True,
                tainted=bool(review.tainted),
                blocked=bool(review.blocked),
                severity=str(review.severity),
            )
        )
    del considered  # the count is deliberately not published; see ENUMERATION_POLICY
    if truncated:
        notes.append(
            f"truncated at the declared bound (results_cap={count_cap}, "
            f"bytes_cap={byte_cap}); this is a PARTIAL list and must not be "
            f"read as the whole answer"
        )
    notes.append(
        "executed_anything=False: this function holds no dispatcher and loaded "
        "no schema; a schema is fetched only by the loader that owns it"
    )
    return DiscoveryResult(
        query=str(query or ""),
        hits=tuple(hits),
        returned=len(hits),
        truncated=truncated,
        bytes=total_bytes,
        results_cap=count_cap,
        bytes_cap=byte_cap,
        executed_anything=False,
        enumeration=ENUMERATION_POLICY,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 4. Skills may narrow; they may never widen
# ---------------------------------------------------------------------------

#: A CLOSED set of trust tiers. An unknown tier is not "trusted": a skill from a
#: directory nobody vouched for must not be able to reach the operator's
#: allow-claims by being spelled differently.
SKILL_TRUST_TIERS: Tuple[str, ...] = ("trusted", "untrusted")

#: Origins ``harness.skills.Skill`` records. Project content is repository
#: controlled, so it is the UNTRUSTED tier; a plugin is a third-party bundle.
TRUSTED_SKILL_ORIGINS: Tuple[str, ...] = ("global",)


def skill_source_is_trusted(origin: Any) -> bool:
    """Return whether a skill's origin is in the trusted tier.

    Reads ``TRUSTED_SKILL_ORIGINS``, which is a subset of the origins
    ``harness.skills.Skill`` already assigns. An unknown origin is UNTRUSTED —
    the direction that cannot widen.
    """
    return str(origin or "").strip().casefold() in TRUSTED_SKILL_ORIGINS


def required_binaries_present(binaries: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """Return the declared binaries that are NOT resolvable on ``PATH``.

    ``shutil.which`` is the resolution the product uses everywhere else; an
    absolute path is checked with ``os.path.isfile`` so a skill declaring a
    binary by path is answered the same way rather than by a second rule.
    """
    missing: List[str] = []
    for raw in binaries or ():
        name = str(raw or "").strip()
        if not name:
            continue
        if os.path.isabs(name):
            found = os.path.isfile(name) and os.access(name, os.X_OK)
        else:
            found = shutil.which(name) is not None
        if not found:
            missing.append(name)
    return tuple(missing)


@dataclass(frozen=True)
class SkillCapabilityGrant:
    """What a skill may actually use, after every intersection.

    ``effective_tools`` is a SUBSET of ``run_tools`` — always, not because a
    caller passed the right thing, because :func:`grant_skill_capabilities`
    intersects with ``run_tools`` itself. A skill cannot widen by declaration,
    by tier, by quarantine, or by declaring a binary that is not installed.
    """

    skill: str
    tier: str
    declared_tools: Tuple[str, ...] = ()
    effective_tools: Tuple[str, ...] = ()
    run_tools: Tuple[str, ...] = ()
    refused: Tuple[Dict[str, Any], ...] = ()
    quarantined: bool = False
    loaded: bool = True
    unsatisfied_requirements: Tuple[str, ...] = ()
    effective_model_tier: str = ""
    notes: Tuple[str, ...] = ()

    @property
    def narrowed_only(self) -> bool:
        """Whether the effective set is a subset of the run's own surface."""
        return set(self.effective_tools) <= set(self.run_tools)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "skill": self.skill,
            "tier": self.tier,
            "declared_tools": list(self.declared_tools),
            "effective_tools": list(self.effective_tools),
            "run_tools": list(self.run_tools),
            "refused": [dict(item) for item in self.refused],
            "quarantined": bool(self.quarantined),
            "loaded": bool(self.loaded),
            "unsatisfied_requirements": list(self.unsatisfied_requirements),
            "effective_model_tier": self.effective_model_tier,
            "narrowed_only": bool(self.narrowed_only),
            "notes": list(self.notes),
        }


def grant_skill_capabilities(
    *,
    skill: str,
    declared_tools: Optional[Sequence[str]] = None,
    declared_model_tier: str = "",
    run_tools: Optional[Sequence[str]] = None,
    session: Optional[Any] = None,
    tier: str = "trusted",
    quarantined: bool = False,
    required_binaries: Optional[Sequence[str]] = None,
) -> SkillCapabilityGrant:
    """Intersect a skill's declared tools with the run's, and refuse the rest.

    The five rules, in the order they can narrow:

    1. **A missing binary means the skill is not loaded.** ``loaded=False`` and
       ``effective_tools=()``. An unsatisfied runtime requirement is not a
       degraded load — the skill's instructions describe a toolchain this host
       does not have.
    2. **Quarantine is inert.** A quarantined skill grants nothing at all, and
       says so rather than returning an empty set a caller could mistake for
       "it declared nothing".
    3. **Trust tier bounds CAPABILITY, not just trust.** An untrusted skill is
       capped at ``run_tools`` — the tools the run itself exposes — and never
       reads the operator's ``allow`` claims, because a repository-supplied
       ``SKILL.md`` reaching an operator-declared grant is the widening this
       rule exists to stop. A trusted skill still gets the normal envelope
       intersection.
    4. **A declared tool the run lacks is refused and NAMED.** Silent dropping
       is indistinguishable from a skill that declared nothing.
    5. **The effective set is intersected with ``run_tools`` one final time**,
       so no upstream bug can widen it. A declared model tier is clamped by
       :func:`extensions.skill_policy.clamp_tier`, which has no input that raises
       a ceiling.

    ``extensions.skill_policy`` is DELEGATED to for the envelope intersection
    (``resolve_tools`` / ``clamp_tier``); this module owns no second copy of that
    rule, which is why it imports rather than restating it.
    """
    name = str(skill or "")
    run = _clean(run_tools)
    declared = _clean(declared_tools)
    tier_name = str(tier or "").strip().casefold()
    if tier_name not in SKILL_TRUST_TIERS:
        tier_name = "untrusted"
    refused: List[Dict[str, Any]] = []
    notes: List[str] = []

    missing = required_binaries_present(required_binaries)
    if missing:
        notes.append(
            f"runtime requirement not satisfied on this host: {list(missing)}; "
            f"the skill was not loaded and grants nothing"
        )
        return SkillCapabilityGrant(
            skill=name,
            tier=tier_name,
            declared_tools=declared,
            effective_tools=(),
            run_tools=run,
            quarantined=bool(quarantined),
            loaded=False,
            unsatisfied_requirements=missing,
            effective_model_tier="",
            notes=tuple(notes),
        )

    if quarantined:
        notes.append(
            "the skill was quarantined by the untrusted-content review, so its "
            "declarations are inert: an inert declaration is not an empty "
            "declaration, and this grant says which one it is"
        )
        return SkillCapabilityGrant(
            skill=name,
            tier=tier_name,
            declared_tools=declared,
            effective_tools=(),
            run_tools=run,
            quarantined=True,
            loaded=False,
            unsatisfied_requirements=(),
            effective_model_tier="",
            notes=tuple(notes),
        )

    envelope = session
    ceiling_tier = "hard"
    if tier_name == "untrusted":
        from extensions.skill_policy import SessionPermissions

        envelope = SessionPermissions(allow_tools=run, max_model_tier=ceiling_tier)
        notes.append(
            "untrusted tier: the skill is capped at the run's own visible tool "
            "surface and does not read the operator's allow-claims"
        )

    effective: Tuple[str, ...] = ()
    tier_ceiling = ""
    try:
        from extensions.skill_policy import clamp_tier, resolve_tools
    except Exception as exc:  # pragma: no cover - extensions is optional
        notes.append(f"extensions.skill_policy is unavailable: {exc}")
    else:
        granted, tool_refusals = resolve_tools(declared, envelope or ())
        refused.extend(dict(item) for item in tool_refusals)
        effective = granted
        tier_ceiling, clamp_reason = clamp_tier(
            declared_model_tier or "medium", ceiling_tier
        )
        if clamp_reason:
            notes.append(clamp_reason)

    # (5) The final, unconditional narrowing. Everything above can only remove.
    narrowed = tuple(item for item in effective if item in run)
    for dropped in effective:
        if dropped not in run:
            refused.append(
                {
                    "tool": dropped,
                    "reason": "not exposed by this run's own tool surface",
                }
            )
    return SkillCapabilityGrant(
        skill=name,
        tier=tier_name,
        declared_tools=declared,
        effective_tools=narrowed,
        run_tools=run,
        refused=tuple(refused),
        quarantined=False,
        loaded=True,
        unsatisfied_requirements=(),
        effective_model_tier=tier_ceiling,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 5. The structural claim: a description cannot grant
# ---------------------------------------------------------------------------

#: AST node names a grant would have to be written with. Checked against THIS
#: module's own source by :func:`forbidden_grant_expressions`, which the pin
#: module calls — so "we were careful" becomes "there is no expression for it".
_FORBIDDEN_ATTR_WRITES: Tuple[str, ...] = (
    "requires_approval",
    "sandboxed",
    "mutating",
    "network",
    "process",
    "requires_human",
)
_FORBIDDEN_CALLS: Tuple[str, ...] = ("replace", "setattr", "dataclasses.replace")


def forbidden_grant_expressions(source: str) -> Tuple[str, ...]:
    """Return every AST site in ``source`` that could express a permission grant.

    A grant is one of three things: an assignment to a ``ToolPolicy`` field, a
    call to ``dataclasses.replace``/``setattr`` on a policy, or a write to the
    module's policy table. This returns each site as a readable string so a
    failing assertion can name the LINE rather than saying "a grant exists".

    ``policy_for_tool`` is the one permitted name and is explicitly excluded: it
    is the read, and it is the authority this module is built on.
    """
    findings: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - defensive
        return (f"source did not parse: {exc}",)
    for node in ast.walk(tree):
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            # `x: bool` with NO value is a DECLARATION. A declaration cannot
            # grant anything, and `ExecutionPathFingerprint` has to be able to
            # name the fields it reports — a check that flagged those would be
            # a check that could only be satisfied by not reporting the fields.
            if node.value is not None:
                targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            names: List[str] = []
            if isinstance(target, ast.Attribute):
                names = [target.attr]
            elif isinstance(target, ast.Name):
                names = [target.id]
            for name in names:
                if name in _FORBIDDEN_ATTR_WRITES:
                    findings.append(
                        f"line {node.lineno}: assigns to {name!r}, a "
                        f"ToolPolicy field that only policy_for_tool may set"
                    )
                if name == "_TOOL_POLICIES":
                    findings.append(f"line {node.lineno}: writes to the policy table")
        if isinstance(node, ast.Call):
            func = node.func
            label = ""
            if isinstance(func, ast.Attribute):
                label = func.attr
            elif isinstance(func, ast.Name):
                label = func.id
            if label in _FORBIDDEN_CALLS:
                findings.append(
                    f"line {node.lineno}: calls {label}(), which can rewrite a "
                    f"policy in place"
                )
    return tuple(findings)


def module_source() -> str:
    """Return this module's own source, for the structural self-check."""
    import pathlib

    return pathlib.Path(__file__).read_text(encoding="utf-8")


#: The executables a discovery path must never be able to name. Checked against
#: :func:`executable_names`, i.e. against CODE, so this module's own prose about
#: ``SafeToolBackend`` — which is most of its first paragraph — cannot fail its
#: own check. A scan that a docstring can satisfy is not a scan.
#:
#: Deliberately NARROW. A first draft included ``run`` and ``call``, which are
#: ordinary English identifiers: this module has a local named ``run`` and three
#: ``run=`` keywords, and a check that fires on those is a check that gets
#: deleted rather than satisfied. Every name below is one that could not appear
#: in working code here.
EXECUTABLE_FORBIDDEN: Tuple[str, ...] = (
    "execute_sandboxed",
    "execute_typed_tool",
    "execute",
    "start_local_execution",
    "call_mcp_tool",
    "Popen",
    "popen",
    "check_output",
    "system",
    "exec",
    "eval",
    "spawn",
    "import_module",
    "__import__",
)


def executable_names(source: str) -> Tuple[str, ...]:
    """Return every identifier and string literal in ``source`` that EXECUTES.

    Docstrings and comments are excluded structurally rather than by string
    matching: docstrings are ``ast.Expr(Constant(str))`` in a body position, and
    this returns a bare string constant only when it is not the sole expression
    of a statement — which is where docstrings live by definition.
    """
    names: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    docstring_nodes = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstring_nodes.add(id(value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
            names.append(node.arg)
        elif isinstance(node, ast.alias):
            names.append(node.name.split(".")[-1])
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_nodes
        ):
            names.append(node.value)
    return tuple(names)


def forbidden_execution_names(source: str) -> Tuple[str, ...]:
    """Return the executable names in ``source`` that could run something.

    ``run``/``call``/``exec``/``eval`` are the shapes a hand-rolled dispatcher
    would use, and they are ordinary words that appear in prose, so the check is
    over CODE and says which line each hit is on.
    """
    hits: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - defensive
        return (f"source did not parse: {exc}",)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in EXECUTABLE_FORBIDDEN:
            hits.append(f"line {node.lineno}: reads the name {node.id!r}")
        elif isinstance(node, ast.Attribute) and node.attr in EXECUTABLE_FORBIDDEN:
            hits.append(f"line {node.lineno}: reads the attribute {node.attr!r}")
    return tuple(hits)
