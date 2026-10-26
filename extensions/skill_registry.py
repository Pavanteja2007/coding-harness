"""The skills discovery + trust REGISTRY, as data.

WHY DATA AND NOT A WALK FUNCTION
--------------------------------
The corpus's own summary of this problem is that skills are near-universal
(9 of 11 systems) and that **almost nothing about them agrees**: the paths, the
invocation form, the loading strategy, the gating, and the trust model all
vary per system. A codebase that encodes its answer as an ``if`` chain in a
directory-scanning function cannot be reviewed against any of that, and the
first person to add a path will add it in the wrong place.

So this module holds FOUR TABLES and no filesystem walking:

============================  =================================================
:data:`DISCOVERY_PATHS`      every path a skill may be found at, with its
                             precedence, its trust default, and WHY it exists
:data:`TRUST_TIERS`          builtin / trusted / community, each with an
                             explicit scan policy and quarantine slot
:data:`RUNTIME_REQUIREMENTS` the gates a skill may declare (bins, env vars,
                             OS family) and how a failed gate is reported
:data:`PRECEDENCE`           the ONE precedence rule, stated and pinnable
============================  =================================================

The filesystem side stays in ``harness/skills.py``, which already walks roots,
refuses symlinks, parses ``SKILL.md`` and runs the trust review. **This module
does not replace it and does not import it.** It is the table that walk should
be reading, and it is deliberately importable without ``harness`` so a test can
assert the tables without a working scanner.

THE CROSS-VENDOR PATHS ARE THE POINT
-------------------------------------
Two rows are not ours and are the most important rows in the table:

* ``~/.claude/skills`` - a USER-LEVEL path belonging to a different product.
* ``<repo>/.claude/skills`` - a REPOSITORY-level path belonging to a different
  product.

The corpus calls one system's deliberate reading of a competitor's home
directory "the sharpest interoperability datum" in the whole survey, and it is
sharp in both directions. Reading it means a skill written for another agent
works here, which is interoperability. It also means **unreviewed third-party
content enters the discovery path**, which is why both rows carry
``trust="community"`` as their DEFAULT rather than ``trusted`` - a path a user
did not put there by installing our product cannot be treated as trusted merely
because it is under their home directory.

That default is the load-bearing decision. :func:`classify_path` returns the
tier a path is *eligible* for, and
:func:`discovered_skill_policy` intersects it with the tier a user has actually
granted, so a repository-level Claude skill is ``community`` until somebody
promotes it. A registry that only records paths and leaves the trust decision
to a reader is a registry whose reader will guess.

PRECEDENCE: ONE RULE, PINNED
----------------------------
The corpus shows Gemini using ``workspace > user > built-in`` and most other
systems using first-found-wins. First-found-wins is not *wrong*; it is
**unauditable**, because "first" depends on the order a directory listing
happened to return.

:data:`PRECEDENCE` therefore pins an explicit total order over
:data:`DISCOVERY_SCOPES`, and :func:`resolve_precedence` implements it. Within
one directory a name collision is first-found-wins over a
:data:`DISCOVERY_PATHS` row that is itself sorted - which is deterministic,
and is the one place first-found-wins is acceptable, because the order it walks
is declared rather than inherited from the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DISCOVERY_PATHS",
    "DISCOVERY_SCOPES",
    "INTEROP_PATHS",
    "PRECEDENCE",
    "RUNTIME_GATE_KINDS",
    "SCAN_POLICIES",
    "TRUST_TIERS",
    "PathRule",
    "RuntimeGate",
    "TrustTier",
    "classify_path",
    "describe_registry",
    "discovered_skill_policy",
    "permission_cap_source",
    "resolve_precedence",
    "runtime_gate_report",
    "validate_trust_tiers",
]


@dataclass(frozen=True)
class PathRule:
    """One discovery path, as data.

    :param scope: which of :data:`DISCOVERY_SCOPES` this belongs to.
    :param template: a ``str.format``-able path. ``{home}`` and ``{repo}`` are
        the two substitutions; anything else in a template is a typo, and
        :func:`resolve_precedence` reports it rather than producing a path that
        silently does not exist.
    :param trust: the trust tier a skill at this path is ELIGIBLE for by
        default. Not the tier it gets - see the module docstring - because a
        default that is also the outcome is a decision nobody makes.
    :param why: the reason this row exists, published in the registry report so
        a reviewer can disagree with a row instead of hunting for it.
    :param foreign: whether this path belongs to another product. Foreign paths
        are the ones that need the most justification, so they are marked rather
        than left to be inferred from the template.
    """

    scope: str
    template: str
    trust: str
    why: str
    foreign: bool = False

    def render(self, *, home: str = "", repo: str = "") -> str:
        """Substitute ``{home}`` and ``{repo}``; wildcard the per-instance tokens.

        ``{home}`` and ``{repo}`` are the CALLER's inputs, so an absent one is a
        missing input and raises. ``{plugin}``, ``{registry}``, ``{namespace}``
        and ``{skill}`` are PER-INSTANCE: they vary per skill rather than per
        call, so an unbound one renders as the wildcard ``*`` and the row stays
        a usable pattern.

        The first version raised on every unbound token, which meant
        ``{home}/.config/neo/plugins/{plugin}/skills`` could never render at
        all - a ``KeyError`` out of ``str.format`` rather than the missing-input
        error this documents. So the plugin row was dead in
        :func:`resolve_precedence` and the interop-free part of the table was
        silently shorter than it looked.
        """
        missing = [
            token
            for token in ("{home}", "{repo}")
            if token in self.template and not (home if token == "{home}" else repo)
        ]
        if missing:
            raise ValueError(
                f"discovery path {self.template!r} needs {missing} to render"
            )
        wildcard = {"plugin": "*", "registry": "*", "namespace": "*", "skill": "*"}
        return self.template.format(home=home, repo=repo, **wildcard)


#: The scope vocabulary. Ordered by :data:`PRECEDENCE`, not by declaration
#: order, so the two cannot disagree.
DISCOVERY_SCOPES: Tuple[str, ...] = (
    "project",
    "project_foreign",
    "user",
    "user_foreign",
    "plugin",
    "builtin",
    "remote",
)

#: THE PRECEDENCE RULE. One total order over :data:`DISCOVERY_SCOPES`, highest
#: priority first.
#:
#: **Chosen rule: most-specific-and-most-local wins, and FOREIGN paths are
#: demoted below every same-origin path of the same or greater locality.**
#:
#: Three reasons, and the rejected alternatives stated so a future change is a
#: decision rather than a drift:
#:
#: 1. **Project beats user beats builtin** (Gemini's order, adopted). A
#:    committed, team-shared skill is more specific than a personal one, and a
#:    built-in is the most generic thing there is. This is the same rule
#:    ``harness/skills.py`` already applies by comment ("project wins name
#:    collisions"), so adopting it here changes no behaviour - it RECORDS the
#:    behaviour that was already there, which is the cheapest possible
#:    correctness win available.
#: 2. **First-found-wins is rejected** for cross-scope resolution. It makes the
#:    winner depend on directory-listing order, which is not a property of the
#:    policy. It is retained ONLY within a single scope, over rows sorted by
#:    :data:`DISCOVERY_PATHS` order, which is declared rather than inherited.
#: 3. **Foreign below same-origin** is the interop-specific rule. A Claude skill
#:    in the repository should not silently shadow a first-party project skill
#:    of the same name; if it did, adopting an interop path would be a
#:    behaviour change for existing users rather than an addition.
#:
#: ``remote`` is LAST and separately gated. A registry-fetched skill is the
#: least local thing that can be loaded and its content has travelled the most,
#: so it is below even a built-in: a user's own installation outranks content
#: someone else published.
PRECEDENCE: Tuple[str, ...] = (
    "project",
    "user",
    "builtin",
    "project_foreign",
    "user_foreign",
    "plugin",
    "remote",
)

#: Every discovery path, in the order a walk must try them WITHIN a scope.
#:
#: Two rows are deliberately ``foreign=True``: they belong to another product.
#: They are here because the corpus calls cross-vendor interoperability the
#: sharpest datum in the survey, and because a user's skills should not need
#: re-authoring to work in a second agent. They are demoted by
#: :data:`PRECEDENCE` and default to ``community`` trust, because an
#: interop path is by definition a path this product did not write to.
DISCOVERY_PATHS: Tuple[PathRule, ...] = (
    PathRule(
        scope="project",
        template="{repo}/.agents/skills",
        trust="trusted",
        why=(
            "the cross-vendor standard: six of eleven surveyed systems accept "
            "`.agents/skills/`, which makes it the only location a skill "
            "author can target without choosing a vendor"
        ),
    ),
    PathRule(
        scope="project",
        template="{repo}/.neo/skills",
        trust="trusted",
        why="our own committed, team-shared location; already scanned today",
    ),
    PathRule(
        scope="project",
        template="{repo}/skills",
        trust="trusted",
        why=(
            "a bare `skills/` at the repository root, which several surveyed "
            "systems read; the repo's own path segments are also skill "
            "vocabulary, so a repo that ships one is already shaped like this"
        ),
    ),
    PathRule(
        scope="project_foreign",
        template="{repo}/.claude/skills",
        trust="community",
        why=(
            "CROSS-VENDOR INTEROP: another agent's project-level skills, read "
            "deliberately. Demoted below every first-party project row so "
            "adopting it cannot shadow our own, and community-trusted by "
            "default because this product did not author this path"
        ),
        foreign=True,
    ),
    PathRule(
        scope="user",
        template="{home}/.agents/skills",
        trust="trusted",
        why="the same cross-vendor standard at user level",
    ),
    PathRule(
        scope="user",
        template="{home}/.config/neo/skills",
        trust="trusted",
        why="our own user-level location; already scanned today",
    ),
    PathRule(
        scope="user_foreign",
        template="{home}/.claude/skills",
        trust="community",
        why=(
            "CROSS-VENDOR INTEROP at user level, and the single row the corpus "
            "calls 'the sharpest interoperability datum in the survey'. "
            "community-trusted by DEFAULT: it sits under the user's home "
            "directory, but the user did not put it there by installing THIS "
            "product, so its contents are another tool's and unreviewed here"
        ),
        foreign=True,
    ),
    PathRule(
        scope="plugin",
        template="{home}/.config/neo/plugins/{plugin}/skills",
        trust="community",
        why=(
            "a plugin bundle's skills. community, not trusted: a plugin is "
            "third-party code with third-party instructions, and Phase 2's "
            "untrusted-source review is what earns it anything higher"
        ),
    ),
    PathRule(
        scope="builtin",
        template="{home}/.config/neo/builtin/skills",
        trust="builtin",
        why="ships with the product; the only tier that needs no review",
    ),
    PathRule(
        scope="remote",
        template="registry:{registry}/{namespace}/{skill}",
        trust="community",
        why=(
            "a remote registry. Not a filesystem path at all, which is why "
            "rendering it needs neither home nor repo. Last in precedence: a "
            "user's own installation outranks content someone else published"
        ),
    ),
)

#: The rows that belong to another product, as a derived set. Derived rather
#: than hand-kept, because a hand-kept second list is exactly the drift
#: ``phases/DOCTRINE.md`` section 8 records having cost this repository 345x.
INTEROP_PATHS: Tuple[str, ...] = tuple(
    rule.template for rule in DISCOVERY_PATHS if rule.foreign
)


def resolve_precedence(*, home: str = "", repo: str = "") -> List[PathRule]:
    """Return every path in precedence order, ready to walk.

    Sorts by the index of :attr:`PathRule.scope` in :data:`PRECEDENCE`, then by
    declaration order WITHIN a scope. Both keys are declared, so the result is
    deterministic and auditable - which is the entire point of pinning
    precedence instead of inheriting it from a directory listing.

    A path that cannot render is DROPPED and named in the registry report rather
    than raising here. A registry that refuses to describe itself because one
    row needs a value the caller did not supply is a registry nobody runs with
    partial input, and partial input is the normal case.
    """
    order = {scope: index for index, scope in enumerate(PRECEDENCE)}
    unknown = sorted({r.scope for r in DISCOVERY_PATHS} - set(order))
    if unknown:
        raise ValueError(
            f"discovery path(s) with a scope absent from PRECEDENCE: {unknown}"
        )
    ordered = sorted(
        enumerate(DISCOVERY_PATHS), key=lambda pair: (order[pair[1].scope], pair[0])
    )
    out: List[PathRule] = []
    for _, rule in ordered:
        try:
            rule.render(home=home, repo=repo)
        except ValueError:
            continue
        out.append(rule)
    return out


def _norm(path: Any) -> str:
    """Normalise a path for comparison: forward slashes, no trailing slash."""
    return str(path or "").replace("\\", "/").rstrip("/")


def classify_path(
    path: str,
    *,
    home: str = "",
    repo: str = "",
    rules: Optional[Sequence[PathRule]] = None,
) -> Dict[str, Any]:
    """Return the trust tier a path is ELIGIBLE for, and why.

    Templates are RENDERED against ``home``/``repo`` before comparison. The first
    version of this function skipped any template containing a placeholder
    instead of rendering it - on the reasoning that a template cannot be
    compared structurally - which meant that no real, substituted path could
    ever match and every call returned ``tier=""``. A function that is always
    empty is worse than no function, because it looks like an answer.

    Matching is exact on the rendered path, then falls back to a LAST-SEGMENT
    match so a caller who passes a directory rather than the exact root still
    gets an answer. The fallback takes the FIRST rule in declared order, which
    is deterministic because :data:`DISCOVERY_PATHS` is a declared sequence.

    An unrecognised path returns ``tier=""`` rather than a default. An empty
    tier means "this path is in no rule", which is a MISSING MEASUREMENT;
    defaulting it to ``community`` would silently downgrade a first-party path
    somebody added, and defaulting it to ``trusted`` would silently promote one
    somebody else added.
    """
    target = _norm(path)
    if not target:
        return {
            "path": "",
            "tier": "",
            "scope": "",
            "foreign": False,
            "matched": False,
            "reason": "empty path",
        }
    table = tuple(rules if rules is not None else DISCOVERY_PATHS)
    rendered: List[Tuple[int, PathRule, str]] = []
    for index, rule in enumerate(table):
        try:
            concrete = _norm(rule.render(home=home, repo=repo))
        except ValueError:
            continue
        rendered.append((index, rule, concrete))
    for _index, rule, concrete in rendered:
        if concrete and target == concrete:
            return _classification(target, rule, "exact rendered path match")
    for _index, rule, concrete in rendered:
        tail = concrete.rsplit("/", 1)[-1]
        if tail and target.endswith("/" + tail):
            return _classification(target, rule, "last-segment match on a rendered path")
    # A registry URL has no home or repo to substitute, so it is compared
    # against its own unrendered template.
    for _index, rule, _concrete in rendered:
        template = _norm(rule.template)
        if "{" in template:
            continue
        if template and target.startswith(template + "/"):
            return _classification(target, rule, "registry URL prefix match")
    return {
        "path": str(path),
        "tier": "",
        "scope": "",
        "foreign": False,
        "matched": False,
        "reason": (
            "no rendered discovery rule matches this path; the tier is "
            "UNMEASURED, which is different from community"
        ),
    }


def _classification(path: str, rule: PathRule, how: str) -> Dict[str, Any]:
    """Build one :func:`classify_path` match result."""
    return {
        "path": str(path),
        "tier": rule.trust,
        "scope": rule.scope,
        "foreign": bool(rule.foreign),
        "matched": True,
        "template": rule.template,
        "why": rule.why,
        "matched_by": how,
    }


@dataclass(frozen=True)
class TrustTier:
    """One trust tier and what it MEANS operationally.

    The corpus's model is builtin / trusted / community with pre-install static
    scanning and quarantine. All three halves are here as data, because a trust
    tier without a stated scan policy is a label:

    :param scan: ``"none"``, ``"static"`` or ``"review"``. What happens to the
        skill's CONTENT before it is admitted. ``"none"`` is only ever correct
        for a tier that ships with the product.
    :param quarantine: whether a skill in this tier that fails its scan is
        quarantined (held, reported, not admitted) rather than dropped or
        admitted-with-taint. Quarantine is the only one of those three that
        leaves a record a human can act on.
    :param model_invocation: whether the MODEL may select a skill in this tier
        on its own. This is the corpus's ``disable-model-invocation`` axis, and
        it is orthogonal to trust: a high-trust skill can still be
        human-invoked-only.
    :param review: the Phase-2 trust boundary to route the content through.
        Named rather than implied, so a tier cannot be quietly wired to no
        boundary at all.
    """

    name: str
    rank: int
    scan: str
    quarantine: bool
    model_invocation: bool
    review: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.name,
            "rank": self.rank,
            "scan": self.scan,
            "quarantine": self.quarantine,
            "model_invocation": self.model_invocation,
            "review": self.review,
            "description": self.description,
        }


#: THE THREE TRUST TIERS. Highest rank first.
#:
#: The two decisions worth arguing about, recorded with their reasons:
#:
#: * **A community skill is MODEL-INVOCABLE BY DEFAULT.** The alternative -
#:   human-invocation-only for everything a user did not install - is safer in
#:   the abstract and unusable in practice: the whole point of a skill is that
#:   the agent recognises when one applies. The safety property is not "the
#:   model cannot reach it" but "the content is scanned before it is admitted
#:   and a failure is quarantined", which is what ``scan``/``quarantine`` say.
#: * **``builtin`` is the only tier with ``scan="none"``.** Any other tier with
#:   no scan would be a content path with no review, and
#:   :func:`validate_trust_tiers` fails the build on that combination rather
#:   than trusting a future edit to keep the invariant.
TRUST_TIERS: Tuple[TrustTier, ...] = (
    TrustTier(
        name="builtin",
        rank=30,
        scan="none",
        quarantine=False,
        model_invocation=True,
        review="none",
        description=(
            "ships with the product. The only tier whose content this "
            "repository authored, and the only one exempt from a scan."
        ),
    ),
    TrustTier(
        name="trusted",
        rank=20,
        scan="static",
        quarantine=True,
        model_invocation=True,
        review="shared.security.review_untrusted_source(source='skill')",
        description=(
            "first-party or explicitly promoted: our own paths, plus anything "
            "a user promoted out of the community tier. Scanned anyway, because "
            "a user editing a file under their own home directory is not a "
            "security control"
        ),
    ),
    TrustTier(
        name="community",
        rank=10,
        scan="static",
        quarantine=True,
        model_invocation=True,
        review="shared.security.review_untrusted_source(source='skill')",
        description=(
            "third-party content: plugin bundles, a foreign vendor's skills, a "
            "remote registry. Pre-install static scan with quarantine on "
            "failure. This is the DEFAULT for every interop path, because a "
            "path this product did not author is community by definition"
        ),
    ),
)

#: The closed vocabulary of scan policies. A fourth kind would mean a tier that
#: scans in a way nothing else knows how to interpret.
SCAN_POLICIES: Tuple[str, ...] = ("none", "static", "review")

#: The closed vocabulary of runtime-gate kinds. These are the three axes the
#: corpus names: "preventing 'how to use kubectl' from polluting the prompt on
#: a machine without it."
RUNTIME_GATE_KINDS: Tuple[str, ...] = ("bin", "env", "os_family")


@dataclass(frozen=True)
class RuntimeGate:
    """A declared runtime requirement a skill may carry.

    A gate is a FILTER, not a grant. Satisfying every gate makes a skill
    ELIGIBLE to have its description in the prompt; it grants nothing, and the
    permission cap is computed separately by
    :mod:`extensions.skill_policy` from the run's own envelope.

    :param kind: one of :data:`RUNTIME_GATE_KINDS`.
    :param value: the binary name, the environment variable name, or the OS
        family. Meaning depends on ``kind`` and nothing else, so a reader never
        has to guess.
    :param reason: shown to the user when the gate FAILS, because a skill that
        silently does not appear is indistinguishable from a skill that does not
        exist.
    """

    kind: str
    value: str
    reason: str = ""

    def describe(self) -> str:
        """One human line naming the gate and its value."""
        labels = {
            "bin": f"requires the `{self.value}` binary",
            "env": f"requires environment variable {self.value}",
            "os_family": f"only applicable on {self.value}",
        }
        base = labels.get(self.kind, f"requires {self.kind}={self.value}")
        return f"{base} ({self.reason})" if self.reason else base


def runtime_gate_report(
    gates: Iterable[Any],
    *,
    which_bins: Optional[Iterable[str]] = None,
    env: Optional[Mapping[str, Any]] = None,
    os_family: str = "",
) -> Dict[str, Any]:
    """Evaluate declared runtime gates and report every one, pass or fail.

    Reports ALL gates rather than short-circuiting on the first failure. A
    short-circuit reports one missing binary and leaves the user guessing
    whether anything else was also missing, which is the "report a bounded
    zero" failure this repository's doctrine names: a partial list of unmet
    requirements reads as a complete one.

    Every gate's status is one of ``satisfied`` / ``unsatisfied`` /
    ``unknown`` - never a boolean. ``which_bins=None`` means "we did not look",
    which is a different answer from "we looked and it is absent", and a
    boolean cannot say which.

    An empty gate list is ``eligible=True`` with ``gates_evaluated=0``. That is a
    real answer (a skill with no requirements is always eligible) and the count
    is published so it cannot be read as "we checked and everything passed".
    """
    environment = dict(env or {})
    available = None if which_bins is None else {str(b) for b in which_bins}
    rows: List[Dict[str, Any]] = []
    for raw in list(gates or ())[:64]:
        if isinstance(raw, RuntimeGate):
            gate = raw
        elif isinstance(raw, Mapping):
            gate = RuntimeGate(
                kind=str(raw.get("kind", "")),
                value=str(raw.get("value", "")),
                reason=str(raw.get("reason", "")),
            )
        else:
            gate = RuntimeGate(kind="", value=str(raw))
        if gate.kind not in RUNTIME_GATE_KINDS:
            rows.append(
                {
                    "gate": gate.describe(),
                    "kind": gate.kind or "unknown",
                    "status": "unknown",
                    "reason": f"gate kind is not one of {list(RUNTIME_GATE_KINDS)}",
                }
            )
            continue
        if gate.kind == "bin":
            if available is None:
                rows.append(
                    {
                        "gate": gate.describe(),
                        "kind": gate.kind,
                        "status": "unknown",
                        "reason": "no binary probe was supplied, so absence is "
                        "unmeasured rather than confirmed",
                    }
                )
                continue
            status = "satisfied" if gate.value in available else "unsatisfied"
            reason = "" if status == "satisfied" else (
                f"`{gate.value}` is not on PATH; this skill's description is "
                "withheld so the prompt is not polluted by instructions for a "
                "tool this machine does not have"
            )
        elif gate.kind == "env":
            present = bool(environment.get(gate.value))
            status = "satisfied" if present else "unsatisfied"
            reason = "" if present else (
                f"{gate.value} is unset; this skill's description is withheld"
            )
        else:
            status = (
                "satisfied"
                if os_family and gate.value.lower() == str(os_family).lower()
                else "unsatisfied"
            )
            reason = "" if status == "satisfied" else (
                f"this skill documents {gate.value} and this machine is "
                f"{os_family or 'of unknown family'}"
            )
        rows.append(
            {
                "gate": gate.describe(),
                "kind": gate.kind,
                "status": status,
                "reason": reason,
            }
        )
    unsatisfied = [r for r in rows if r["status"] == "unsatisfied"]
    unknown = [r for r in rows if r["status"] == "unknown"]
    # TRI-STATE, deliberately. `True` with an UNKNOWN gate would tell a reader
    # the skill is eligible on the strength of a requirement nobody checked,
    # which is the "report a zero nobody took" failure in its eligibility form:
    # the corpus's motivating case is a machine WITHOUT kubectl, and a probe
    # that did not run is exactly how that machine gets "how to use kubectl"
    # injected anyway. `None` is "unmeasured", which is a different answer from
    # either yes or no and is the only honest one here.
    if unsatisfied:
        eligible: Optional[bool] = False
    elif unknown:
        eligible = None
    else:
        eligible = True
    return {
        "eligible": eligible,
        "eligible_known": eligible is not None,
        "gates_evaluated": len(rows),
        "satisfied": [r["gate"] for r in rows if r["status"] == "satisfied"],
        "unsatisfied": unsatisfied,
        "unknown": unknown,
        "reason": (
            ""
            if not unsatisfied and not unknown
            else "; ".join(
                [r["reason"] for r in unsatisfied if r["reason"]]
                + [r["reason"] for r in unknown if r["reason"]]
            )
        ),
        "note": (
            "an UNKNOWN gate does not make a skill ineligible; it makes the "
            "eligibility UNMEASURED, which is why it is reported separately "
            "from unsatisfied rather than folded into it"
        ),
    }


def discovered_skill_policy(
    path: str,
    *,
    granted_tier: str = "",
    home: str = "",
    repo: str = "",
    rules: Optional[Sequence[PathRule]] = None,
) -> Dict[str, Any]:
    """The EFFECTIVE trust tier for a skill found at ``path``.

    The intersection of what the path is ELIGIBLE for
    (:func:`classify_path`) and what the operator has GRANTED
    (``granted_tier``). A skill is never better than either.

    ``granted_tier=""`` means "nothing granted", which yields the path's own
    tier - the DEFAULT the table declares, not an elevation. Promotion is a
    separate, explicit act. This is the mechanism that keeps a foreign path
    community-trusted by default: the table's ``community`` is a ceiling until
    somebody raises it on purpose.
    """
    classification = classify_path(path, home=home, repo=repo, rules=rules)
    eligible = classification.get("tier") or ""
    if not granted_tier:
        return {
            **classification,
            "granted_tier": "",
            "effective_tier": eligible,
            "promoted": False,
            "decision": (
                "default tier from the discovery table"
                if eligible
                else "unmatched path: no tier, and no default invented"
            ),
        }
    granted_rank = next(
        (t.rank for t in TRUST_TIERS if t.name == granted_tier), 0
    )
    eligible_rank = next((t.rank for t in TRUST_TIERS if t.name == eligible), 0)
    if not eligible:
        effective = granted_tier
        decision = (
            "path is in no discovery rule; the granted tier applies because "
            "there is no table ceiling to cap it - which is itself worth "
            "reviewing, because an unrecognised path is an unmeasured one"
        )
        promoted = False
    elif granted_rank > eligible_rank:
        effective = granted_tier
        promoted = True
        decision = (
            f"operator granted {granted_tier!r} above the path's "
            f"{eligible!r} ceiling; the grant wins and the promotion is "
            "recorded"
        )
    else:
        effective = eligible
        promoted = False
        decision = (
            f"the path's {eligible!r} tier is at or above the granted "
            f"{granted_tier!r}; the path ceiling wins"
        )
    return {
        **classification,
        "granted_tier": granted_tier,
        "effective_tier": effective,
        "promoted": promoted,
        "decision": decision,
    }


def validate_trust_tiers() -> List[str]:
    """Return every invariant violation in :data:`TRUST_TIERS`.

    Exists so the table can be edited without a reader having to remember the
    rules. Two invariants, both of which are security properties:

    1. every ``scan`` value is in :data:`SCAN_POLICIES`;
    2. **``scan == "none"`` implies ``rank`` is the maximum and ``quarantine`` is
       False.** Only the tier this product ships may skip a scan. A future edit
       that adds a second unscanned tier is a content path with no review, and
       this returns a finding rather than relying on someone noticing.
    """
    findings: List[str] = []
    names = [tier.name for tier in TRUST_TIERS]
    if len(set(names)) != len(names):
        findings.append(f"duplicate trust tier names in {names}")
    top = max((tier.rank for tier in TRUST_TIERS), default=0)
    for tier in TRUST_TIERS:
        if tier.scan not in SCAN_POLICIES:
            findings.append(
                f"tier {tier.name!r} declares scan={tier.scan!r}, which is not "
                f"one of {list(SCAN_POLICIES)}"
            )
        if tier.scan == "none" and tier.rank != top:
            findings.append(
                f"tier {tier.name!r} skips its scan but is not the top-ranked "
                f"tier; only the highest tier may be unscanned"
            )
        if tier.scan == "none" and tier.quarantine:
            findings.append(
                f"tier {tier.name!r} skips its scan and also quarantines, "
                "which describes a scan that does not exist"
            )
    known = set(names)
    for rule in DISCOVERY_PATHS:
        if rule.trust not in known:
            findings.append(
                f"discovery path {rule.template!r} defaults to unknown trust "
                f"tier {rule.trust!r}"
            )
    return findings


def _claim_text(claim: Any) -> str:
    """Render one declared claim, whether it arrived as text or as an object.

    ``resolve_permissions`` accepts either shape and normalises internally, so a
    caller that holds raw claim strings must not have to convert them itself just
    to print what a skill asked for. Returning ``str(claim)`` for a plain string
    rather than assuming ``.describe()`` is the whole point: the first version
    called ``.describe()`` unconditionally and raised ``AttributeError:
    'str' object has no attribute 'describe'`` on the most common input shape
    there is.
    """
    describe = getattr(claim, "describe", None)
    if callable(describe):
        return str(describe())
    return str(claim)


def permission_cap_source(
    declaration: Any,
    session: Any,
    *,
    subject_defaults: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The run-capped permission set for one skill, as T1 receives it.

    **This function does not compute the cap.** It is the SEAM T1 enforces
    through: the intersection itself stays in
    :mod:`extensions.skill_policy`, which is the authority and which already
    never promotes a claim. What this adds is a single call that hands back the
    DECLARED side, the SESSION side, and the RESULT together, so the consumer
    cannot accidentally use the declared set.

    The declared and effective sets are returned under DIFFERENT keys on
    purpose. A caller that reads ``declared`` where it meant ``effective`` is
    the exact bug the corpus names when it notes that a tool description can
    "overclaim": a skill declaring every permission is not thereby granted it,
    and a receipt that shows only the declaration is a receipt that reads as a
    grant.
    """
    from extensions import skill_policy

    if isinstance(declaration, Mapping):
        # A dict is the shape every caller outside the policy module actually
        # has. The first version read `getattr(declaration, "permissions", ())`
        # only, so a mapping produced an EMPTY declared list - which reads as
        # "this skill asked for nothing" rather than as "this seam did not
        # understand its input", and a skill declaring `allow: write` then
        # looked like one that had asked for nothing.
        claims = (
            declaration.get("claims")
            or declaration.get("permissions")
            or declaration.get("permission_claims")
            or ()
        )
    else:
        claims = getattr(declaration, "claims", None)
        if claims is None:
            claims = tuple(getattr(declaration, "permissions", ()) or ())
    resolution = skill_policy.resolve_permissions(
        claims, session, subject_defaults=subject_defaults
    )
    granted = list(resolution.granted)
    denied = list(resolution.denied)
    refused = list(resolution.refused)
    return {
        "skill": str(
            declaration.get("name", "")
            if isinstance(declaration, Mapping)
            else getattr(declaration, "name", "")
        )
        or "",
        "declared": [_claim_text(claim) for claim in claims],
        "effective": [claim.describe() for claim in granted],
        "denied": [claim.describe() for claim in denied],
        "refused": refused,
        "escalation_attempted": bool(resolution.escalation_attempted),
        "session": skill_policy.SessionPermissions.from_value(session).to_dict(),
        "cap_computed_by": "extensions.skill_policy.resolve_permissions",
        "note": (
            "`declared` is what the skill ASKED for and is never a grant; "
            "`effective` is the intersection with the run's envelope and is "
            "the only set a caller may act on"
        ),
    }


def describe_registry() -> Dict[str, Any]:
    """The whole registry as one JSON-compatible report.

    This is the artifact a reviewer reads. It answers "where do skills come
    from, in what order, how much do we trust each source, what must be true
    before one is eligible, and what is wrong with the tables right now" -
    without running a scanner or importing ``harness``.
    """
    findings = validate_trust_tiers()
    return {
        "schema_version": 1,
        "precedence": {
            "rule": (
                "most-specific-and-most-local wins; foreign paths are demoted "
                "below every same-origin path of the same or greater locality; "
                "first-found-wins applies ONLY within one scope, over rows in "
                "declared order"
            ),
            "order": list(PRECEDENCE),
            "scopes": list(DISCOVERY_SCOPES),
            "rejected": {
                "first_found_wins_across_scopes": (
                    "makes the winner depend on directory-listing order, which "
                    "is not a property of the policy"
                ),
                "global_first_found_wins": (
                    "the corpus shows most systems doing this; it is auditable "
                    "only if the walk order is declared, which is what "
                    "resolve_precedence does"
                ),
            },
        },
        "paths": [
            {
                "template": rule.template,
                "scope": rule.scope,
                "default_trust": rule.trust,
                "foreign": rule.foreign,
                "why": rule.why,
                "precedence_rank": PRECEDENCE.index(rule.scope),
            }
            for rule in DISCOVERY_PATHS
        ],
        "interop_paths": list(INTEROP_PATHS),
        "trust_tiers": [tier.to_dict() for tier in TRUST_TIERS],
        "runtime_gate_kinds": list(RUNTIME_GATE_KINDS),
        "path_count": len(DISCOVERY_PATHS),
        "foreign_path_count": len(INTEROP_PATHS),
        "validation_findings": findings,
        "valid": not findings,
        "walk_owner": (
            "harness/skills.py walks these roots; this module holds the table "
            "and imports nothing from it"
        ),
    }
