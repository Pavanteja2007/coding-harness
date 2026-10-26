"""The skill catalog, and whether the agent actually READ each one.

Four states, because "available" was answering a question nobody asked
---------------------------------------------------------------------
A skill on disk is in one of four situations, and collapsing them into
available/not-available is how a run ends up believing it has instructions it
never received:

===============  ===================================================  ===========
state            what it means                                        body read?
===============  ===================================================  ===========
``on_demand``    in the catalog; the body was NOT sent                **no**
``injected``     the body WAS placed in this run's model context      yes
``quarantined``  the untrusted-content boundary refused it            **no**
``ineligible``   it declared a capability this session does not have  **no**
===============  ===================================================  ===========

``capped`` is a fifth flag rather than a fifth state, because a truncated body
is still a body: the skill WAS read, and it was read incompletely. Rendering
that as either "read" or "not read" is one of the two lies, so it is carried
alongside the state and stated.

The inversion this module exists to prevent
-------------------------------------------
:func:`assert_no_skill_claims_a_body_it_did_not_load` fires when a row says
``injected`` and the run's own receipt says the body was not delivered. That
is the same harm as a UI claiming a missing tool is missing: the agent reasons
from a body it never saw, and its answer is confidently wrong. The gate is
checked at the PROJECTION layer and has a control arm in both directions.

Where the facts come from — and where they do not
------------------------------------------------
Every state is read from an authority that already exists:

* **discovery and metadata** — :func:`harness.skills.discover_skills` /
  :func:`harness.skills.skill_catalog`;
* **quarantine and the cap** — the ``review`` record
  (:class:`harness.skills.UntrustedReview`) that discovery already attached;
* **eligibility** — :func:`harness.skills.enforce_skill_policy`, i.e.
  :mod:`extensions.skill_policy`, which intersects a declaration with the
  session's permission envelope. A skill can DECLARE anything and nothing here
  grants it, so this module never decides eligibility itself;
* **delivery** — ``scan["model_content"]`` from
  :func:`harness.skills.build_skill_receipt`'s scan. Delivery is a fact about
  the request, and only the producer can say it.

Nothing here re-derives any of them. A skill catalog built from a second scan
is the "two implementations of one behaviour" defect this repository records
four times over.

Progressive disclosure is a MEASUREMENT here, not a promise
----------------------------------------------------------
"only the name and description load at startup" is worth nothing without the
number, so :func:`disclosure_lines` renders
:func:`harness.skills.progressive_disclosure_report`'s catalog / preloaded /
saving figures and states when the saving is ``None`` — which
``saving_ratio`` is when the preloaded cost is zero, and a ratio with a tiny
denominator is not a percentage anybody should act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "CATALOG_FIELDS",
    "INELIGIBLE_REASONS",
    "SKILL_STATES",
    "SkillRow",
    "SkillView",
    "assert_no_skill_claims_a_body_it_did_not_load",
    "disclosure_lines",
    "skill_lines",
    "skill_view",
]

#: The four states, in reading order. A fifth would be a fifth word for "the
#: agent does not have this", which is the vocabulary problem.
SKILL_STATES: Tuple[str, ...] = ("injected", "on_demand", "quarantined", "ineligible")

#: The catalog columns this view renders. Declared as DATA because a reader
#: needs to know what a row is made of, and a renderer that grows a column
#: nobody declared is how a surface becomes a wall.
CATALOG_FIELDS: Tuple[str, ...] = (
    "name",
    "description",
    "origin",
    "version",
    "model_tier",
    "permission_claims",
    "declared_tools",
)

#: Why a skill is ineligible. Every one of these names something the SESSION
#: lacks, never something the skill did badly — a skill that declared a
#: capability it cannot use is working correctly.
INELIGIBLE_REASONS: Tuple[str, ...] = (
    "no_session_envelope",
    "policy_unavailable",
    "capability_unsatisfied",
    "unparseable_declaration",
)


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    """Read one attribute, or a mapping key, without ever raising.

    A catalog row may be a :class:`harness.skills.Skill` or a dict a caller
    built; supporting both is cheaper than a converter that can be wrong.
    """
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


@dataclass(frozen=True)
class SkillRow:
    """One skill, its metadata, its state, and the sentence explaining it."""

    name: str
    description: str = ""
    origin: str = ""
    source: str = ""
    version: Optional[int] = None
    model_tier: str = ""
    permission_claims: Tuple[str, ...] = ()
    declared_tools: Tuple[str, ...] = ()
    state: str = "on_demand"
    reason: str = ""
    #: Whether the BODY was delivered to the model. ``None`` when the run's
    #: receipt says nothing about delivery — an unknown, not a no.
    body_loaded: Optional[bool] = None
    #: The body was read but CUT. A fifth fact that is neither state nor not.
    capped: bool = False
    cap_reason: str = ""
    body_tokens: Optional[int] = None

    @property
    def injected(self) -> bool:
        return self.state == "injected"

    @property
    def on_demand(self) -> bool:
        return self.state == "on_demand"

    @property
    def quarantined(self) -> bool:
        return self.state == "quarantined"

    @property
    def ineligible(self) -> bool:
        return self.state == "ineligible"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "origin": self.origin,
            "source": self.source,
            "version": self.version,
            "model_tier": self.model_tier,
            "permission_claims": list(self.permission_claims),
            "declared_tools": list(self.declared_tools),
            "state": self.state,
            "reason": self.reason,
            "body_loaded": self.body_loaded,
            "capped": bool(self.capped),
            "cap_reason": self.cap_reason,
            "body_tokens": self.body_tokens,
        }


@dataclass(frozen=True)
class SkillView:
    """Every skill, the counts, and the progressive-disclosure measurement."""

    rows: Tuple[SkillRow, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    #: ``False`` with a reason is a different surface from ``True`` with a zero.
    available: bool = True
    reason: str = ""
    disclosure: Mapping[str, Any] = field(default_factory=dict)
    #: Which names the run's receipt says were DELIVERED. The authority for the
    #: injection check.
    delivered: Tuple[str, ...] = ()
    policy_enforced: bool = True
    policy_reason: str = ""

    def of_state(self, state: str) -> Tuple[SkillRow, ...]:
        return tuple(row for row in self.rows if row.state == str(state))

    @property
    def injected(self) -> Tuple[SkillRow, ...]:
        return self.of_state("injected")

    @property
    def on_demand(self) -> Tuple[SkillRow, ...]:
        return self.of_state("on_demand")

    @property
    def quarantined(self) -> Tuple[SkillRow, ...]:
        return self.of_state("quarantined")

    @property
    def ineligible(self) -> Tuple[SkillRow, ...]:
        return self.of_state("ineligible")

    def counts_line(self) -> str:
        counts = self.counts or {}
        return (
            f"{len(self.rows)} skill(s): "
            + " · ".join(f"{int(counts.get(state, 0))} {state}" for state in SKILL_STATES)
        )

    def false_delivery(self) -> List[Dict[str, str]]:
        """Every way this view claims a body the run never delivered.

        The skill-level inversion, stated as data:

        * a row marked ``injected`` whose name is NOT in the run's delivered
          set — the agent is credited with instructions it never read;
        * ``body_loaded is True`` while the delivered set does not name it —
          the same inversion through the other field;
        * a row in any other state that claims ``body_loaded is True`` — a
          quarantined or ineligible skill cannot have been delivered, so a row
          saying otherwise is a receipt contradicting itself.

        An empty delivered set means the receipt says NOTHING, and contributes
        no problems: absence of evidence is not evidence of absence.
        """
        if not self.delivered:
            return []
        delivered = set(self.delivered)
        problems: List[Dict[str, str]] = []
        for row in self.rows:
            if row.injected and row.name not in delivered:
                problems.append(
                    {
                        "kind": "claimed_injection",
                        "skill": row.name,
                        "detail": (
                            f"{row.name!r} is rendered as injected but the run's receipt "
                            "does not name it as delivered"
                        ),
                    }
                )
            elif row.body_loaded is True and row.name not in delivered:
                problems.append(
                    {
                        "kind": "claimed_body_load",
                        "skill": row.name,
                        "detail": (
                            f"{row.name!r} claims its body was loaded, and the receipt "
                            "does not name it as delivered"
                        ),
                    }
                )
            if row.state != "injected" and row.body_loaded is True:
                problems.append(
                    {
                        "kind": "load_without_state",
                        "skill": row.name,
                        "detail": (
                            f"{row.name!r} is {row.state} yet claims its body was loaded"
                        ),
                    }
                )
        return problems

    def as_dict(self) -> Dict[str, Any]:
        return {
            "available": bool(self.available),
            "reason": self.reason,
            "counts": {state: int((self.counts or {}).get(state, 0)) for state in SKILL_STATES},
            "counts_line": self.counts_line(),
            "rows": [row.as_dict() for row in self.rows],
            "delivered": list(self.delivered),
            "policy_enforced": bool(self.policy_enforced),
            "policy_reason": self.policy_reason,
            "disclosure": dict(self.disclosure),
            "false_delivery": self.false_delivery(),
        }


def _unsatisfied_for(receipt: Optional[Mapping[str, Any]], name: str) -> str:
    """The refusal sentence for one skill, or ``""``.

    Reads ``harness.skills.enforce_skill_policy``'s ``unsatisfied`` list, whose
    rows name what was asked for and why it was refused. A skill absent from
    that list has no declared capability the session lacks — which is not the
    same as "eligible", and this module says so rather than inferring.
    """
    if not isinstance(receipt, Mapping):
        return ""
    for item in receipt.get("unsatisfied") or ():
        if not isinstance(item, Mapping):
            continue
        skill = str(item.get("skill") or item.get("name") or "")
        if skill and skill != name:
            continue
        asked = str(item.get("capability") or item.get("asked") or item.get("claim") or "")
        why = str(item.get("reason") or "")
        parts = [part for part in (asked, why) if part]
        if parts:
            return "; ".join(parts)
    return ""


def _finding_labels(review: Any) -> Tuple[str, ...]:
    """``category/severity`` per finding — never a raw dataclass repr.

    ``UntrustedReview.findings`` rows are MAPPINGS on this build, so a
    ``getattr``-only reader renders every one of them as the word "finding" —
    which is the confident-wrong direction again, just quieter. Both shapes are
    read, and ``evidence`` is deliberately NOT rendered: it is the hostile
    payload, and a receipt describing a finding does not need to reproduce the
    attack.
    """
    if review is None:
        return ()
    out: List[str] = []
    for item in getattr(review, "findings", ()) or ():
        category = str(_attr(item, "category", "") or "")
        severity = str(_attr(item, "severity", "") or "")
        label = "/".join(part for part in (category, severity) if part) or "finding"
        out.append(label)
    return tuple(out)


def skill_view(
    skills: Optional[Iterable[Any]] = None,
    *,
    policy_receipt: Optional[Mapping[str, Any]] = None,
    scan: Optional[Mapping[str, Any]] = None,
    disclosure: Optional[Mapping[str, Any]] = None,
    reason: str = "",
) -> SkillView:
    """Build the four-state skill view. Never raises.

    ``skills`` is whatever discovery returned (a sequence of
    :class:`harness.skills.Skill`, or of dicts). ``policy_receipt`` is
    :func:`harness.skills.enforce_skill_policy`'s output. ``scan`` is the
    scan whose ``model_content`` says what was actually delivered — and
    delivery is the ONLY input that can mark a row ``injected``, because a
    view that decides delivery itself is a view that can claim a body the model
    never saw.

    The precedence is deliberate and worth stating:

    1. **quarantined** — the boundary refused the body. Nothing downstream can
       undo that, so it wins over everything.
    2. **ineligible** — the session lacks a declared capability.
    3. **injected** — the receipt says this body was delivered.
    4. **on_demand** — everything else, which is the honest default: a catalog
       row whose body nobody has read.
    """
    rows_in = list(skills or ())
    delivered: Tuple[str, ...] = ()
    if isinstance(scan, Mapping):
        raw = scan.get("model_content")
        if isinstance(raw, str):
            delivered = ()
        elif isinstance(raw, (list, tuple, set, frozenset)):
            delivered = tuple(str(item) for item in raw)
        elif raw:
            # A truthy non-collection says "something was delivered" without
            # saying what. That is recorded as an empty delivered set, which
            # makes the injection check abstain rather than accuse.
            delivered = ()

    enforced = True
    policy_reason = ""
    if isinstance(policy_receipt, Mapping):
        enforced = bool(policy_receipt.get("enforced", True))
        policy_reason = str(policy_receipt.get("reason") or "")

    rows: List[SkillRow] = []
    for skill in rows_in:
        name = str(_attr(skill, "name", ""))
        review = _attr(skill, "review", None)
        blocked = bool(getattr(review, "blocked", False)) if review is not None else False
        truncated = (
            bool(getattr(review, "truncated", False)) if review is not None else False
        )
        severity = (
            str(getattr(review, "severity", "") or "") if review is not None else ""
        )
        findings = _finding_labels(review)
        unsatisfied = _unsatisfied_for(policy_receipt, name)
        version = _attr(skill, "version", None)
        try:
            version_int: Optional[int] = int(version) if version is not None else None
        except (TypeError, ValueError):
            version_int = None

        if blocked:
            state = "quarantined"
            state_reason = (
                "the untrusted-content boundary refused this body"
                + (f" (severity {severity})" if severity else "")
                + (
                    f": {'; '.join(findings)}"
                    if findings
                    else ""
                )
            )
        elif unsatisfied:
            state = "ineligible"
            state_reason = f"this session does not grant: {unsatisfied}"
        elif name and name in delivered:
            state = "injected"
            state_reason = "the body was placed in this run's model context"
        else:
            state = "on_demand"
            state_reason = "in the catalog; the body loads only when the skill is invoked"

        rows.append(
            SkillRow(
                name=name,
                description=str(_attr(skill, "description", "")),
                origin=str(_attr(skill, "origin", "")),
                source=str(_attr(skill, "source", "")),
                version=version_int,
                model_tier=str(_attr(skill, "model_tier", "")),
                permission_claims=tuple(
                    str(item) for item in (_attr(skill, "permission_claims", ()) or ())
                ),
                declared_tools=tuple(
                    str(item) for item in (_attr(skill, "declared_tools", ()) or ())
                ),
                state=state,
                reason=state_reason,
                # `None` is reserved for the one case we genuinely cannot
                # resolve: a receipt that names nothing, so "nobody was
                # delivered" and "we were not told" are the same bytes. A
                # quarantined or ineligible skill is `False` because the
                # quarantine and the policy are the reason and both are facts.
                body_loaded=(
                    True
                    if state == "injected"
                    else (False if (blocked or unsatisfied or delivered) else None)
                ),
                capped=truncated,
                cap_reason=(
                    "the body exceeded the untrusted-content length cap; the model read a "
                    "TRUNCATED skill and neither knows it"
                    if truncated
                    else ""
                ),
            )
        )

    counts = {state: 0 for state in SKILL_STATES}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    available = bool(rows) or not reason
    if not rows and reason:
        available = False
    return SkillView(
        rows=tuple(rows),
        counts=counts,
        available=available,
        reason=reason or ("" if rows else "no skills were discovered for this repository"),
        disclosure=dict(disclosure or {}),
        delivered=delivered,
        policy_enforced=enforced,
        policy_reason=policy_reason,
    )


def assert_no_skill_claims_a_body_it_did_not_load(view: SkillView) -> None:
    """Raise if the view credits the agent with a body it never received.

    **This is the gate the module exists for.** An agent that believes it read
    a skill reasons from instructions it does not have, and produces a
    confidently wrong answer — the same inversion as a UI claiming a reachable
    capability is missing, and the reason the projection has to be checkable
    rather than trusted.
    """
    problems = view.false_delivery()
    if problems:
        listing = "\n".join(
            f"  {item['kind']}: {item['skill']} - {item['detail']}" for item in problems
        )
        raise AssertionError(
            f"skill view claims {len(problems)} undelivered body(ies):\n{listing}"
        )


# ---------------------------------------------------------------------------
# The renderers — plain lines, every disclosure on its own row
# ---------------------------------------------------------------------------


def _line(text: Any, indent: str = "") -> str:
    """Sanitise, do NOT bound. For a line whose whole job is a disclosure.

    A skill DESCRIPTION and a skill NAME both come from a markdown file this
    process did not write — a plugin directory, a third-party skill — so both
    cross :func:`cli.ui.sanitize_text`. And the reason is not width-bounded:
    ``cli/AGENTS.md`` records four separate disclosures eaten by a width
    bound in this module's own package, and "this body was TRUNCATED" cut in
    half is worse than a wrapped sentence.
    """
    from cli import ui

    return indent + " ".join(str(ui.sanitize_text(text)).split())


def skill_lines(view: SkillView, *, width: int = 78) -> List[str]:
    """Render the skill catalog view. Non-empty for every view, including empty."""
    lines: List[str] = []
    if not view.available:
        lines.append("skills: unavailable")
        lines.append(_line(view.reason or "no skill catalog could be read", "  reason: "))
        lines.append("  this is NOT a repository with no skills: the catalog could not be read")
        return lines

    lines.append(f"skills: {view.counts_line()}")
    if not view.policy_enforced:
        lines.append(
            "  policy NOT enforced: "
            + (view.policy_reason or "no permission envelope was available")
        )
        lines.append(
            "  a declared permission is a REQUEST, and with no envelope nothing was granted"
        )
    for row in view.rows:
        lines.append(_line(row.name, "  - "))
        if row.description:
            lines.append(_line(row.description, "      "))
        meta = [
            f"state: {row.state}",
            f"origin: {row.origin}" if row.origin else "",
            f"v{row.version}" if row.version is not None else "v unknown",
            f"tier: {row.model_tier}" if row.model_tier else "",
            f"claims: {', '.join(row.permission_claims)}" if row.permission_claims else "",
            f"tools: {', '.join(row.declared_tools)}" if row.declared_tools else "",
        ]
        lines.append(_line(" · ".join(part for part in meta if part), "      "))
        lines.append(_line(row.reason, "      why: "))
        if row.body_loaded is None:
            lines.append(
                "      body: this run recorded nothing about whether it loads, so this is "
                "UNKNOWN and not a no"
            )
        elif row.body_loaded:
            lines.append("      body: LOADED into this run's model context")
        else:
            lines.append("      body: not loaded - it loads only when the skill is invoked")
        if row.capped:
            lines.append(_line(row.cap_reason, "      CAPPED: "))
    return lines


def disclosure_lines(view: SkillView, *, width: int = 78) -> List[str]:
    """Render the progressive-disclosure measurement, with its own absent rule."""
    facts = view.disclosure or {}
    lines = ["progressive disclosure"]
    catalog = facts.get("catalog_tokens")
    preloaded = facts.get("preloaded_tokens")
    saving = facts.get("saving_tokens")
    ratio = facts.get("saving_ratio")
    lines.append(
        "  at startup the model sees: "
        + (f"{int(catalog):,} token(s) of catalog" if isinstance(catalog, (int, float)) else "unreported")
    )
    lines.append(
        "  if every body were preloaded: "
        + (f"{int(preloaded):,} token(s)" if isinstance(preloaded, (int, float)) else "unreported")
    )
    if isinstance(saving, (int, float)):
        lines.append(f"  saved: {int(saving):,} token(s)")
    else:
        lines.append("  saved: unreported")
    if isinstance(ratio, (int, float)):
        lines.append(f"  saving ratio: {float(ratio) * 100:.1f}%")
    else:
        # `saving_ratio` is None when the preloaded cost is zero. A ratio with a
        # denominator of zero is not a percentage, and printing one would be the
        # confident-zero defect this whole module is about.
        lines.append(
            "  saving ratio: unreported - the preloaded cost is zero, so there is no ratio "
            "to state (that is NOT 0% and NOT 100%)"
        )
    supporting = facts.get("supporting_loaded")
    if supporting is not None:
        lines.append(
            f"  supporting files loaded on demand: {int(supporting)} (never preloaded)"
        )
    return lines
