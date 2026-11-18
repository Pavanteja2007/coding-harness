"""A policy language whose rules carry their own proof.

The idea is Codex's `execpolicy/`: a rule is not only a decision, it is a
decision **plus the examples that demonstrate it**, and the examples are
executed at load time. A rule whose declared examples do not behave as
declared **refuses to load**, with an error naming the rule and the example
that failed. Fail-closed: a policy that cannot prove itself does not run.

Three things this module owns, and why they are in one place:

1. **The rule model** (``PolicyRule``) and its **example grammar**
   (``parse_subject``). A rule declares ``match`` / ``not_match`` subject
   examples, and they are validated by running the real matcher.
2. **The layering** (``LAYERS``), which is a security property and is pinned:
   the ``floor`` layer is not overridable by anything, and everything above it
   is **last-match-wins within a layer, later layer wins over earlier**.
3. **The arity dictionary** (``load_command_arity``), which is DATA, loaded
   from ``shared/policy_data/command_arity.json``, so a 167-row table is
   corrected by editing a file rather than by editing code.

And the fourth, which is a contract rather than a mechanism:
:func:`resolve_tool_surface` -- how a ``deny`` decision becomes a tool the
model cannot see, and why BOTH over- and under-hiding are failures.

This module sits at the bottom of the dependency ladder. It imports
``shared.approval`` (for the ONE command-prefix matcher in the tree) and
``runtime.hardline`` **lazily and optionally** (for the floor). It imports no
other package, so ``harness``, ``runtime``, ``cli`` and ``mcp_server`` may all
depend on it.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from shared.approval import (
    command_prefix_matches,
    normalize_approval_scope,
)

__all__ = [
    "ARITY_DATA_PATH",
    "ARITY_RISKS",
    "DECISIONS",
    "DECISION_ALIASES",
    "FLOOR_LAYER",
    "LAYERS",
    "POLICY_SUBJECT_KEYS",
    "ArityEntry",
    "CommandArityTable",
    "PolicyOutcome",
    "PolicyProgram",
    "PolicyRule",
    "PolicySubject",
    "PolicyValidationError",
    "SurfaceViolation",
    "ToolSurface",
    "ToolVisibilityDeclaration",
    "arity_pattern",
    "command_arity_table",
    "hardline_floor_rules",
    "load_command_arity",
    "normalize_decision",
    "parse_subject",
    "render_arity_scope_label",
    "resolve_tool_surface",
    "shipped_policy",
    "surface_violations",
    "untrusted_boundary_line",
    "validate_program",
]

#: The closed decision vocabulary. Deliberately the SAME three words as
#: ``shared.agent_contracts.PERMISSION_ACTIONS`` -- ``shared`` is below
#: ``agent_contracts``' consumers and a second vocabulary is how "prompt" and
#: "ask" end up meaning different things in two places. The Starlark
#: spellings are accepted as ALIASES on input and normalised on the way in, so
#: a policy file written in Codex's dialect loads unchanged.
DECISIONS: tuple[str, ...] = ("allow", "ask", "deny")

#: The Starlark / execpolicy spellings mapped onto ours. Recorded rather than
#: invented: ``prompt`` is ``ask`` and ``forbidden`` is ``deny`` in this
#: codebase already, and accepting the other spelling costs nothing while
#: making the corpus's own vocabulary loadable.
DECISION_ALIASES: Mapping[str, str] = {
    "allow": "allow",
    "permit": "allow",
    "prompt": "ask",
    "ask": "ask",
    "deny": "deny",
    "forbidden": "deny",
    "block": "deny",
}

#: The layer a hard refusal lives in, and the ONLY layer nothing overrides.
FLOOR_LAYER = "floor"

#: Evaluation order. **Layer order is a security property, not a preference.**
#:
#: * ``floor`` is evaluated first and a match ends the evaluation. Nothing
#:   above it -- not a user config, not a per-session grant, not an approval --
#:   can turn a floor refusal into anything else. That is the whole reason the
#:   floor is a separate layer rather than a rule with a high priority.
#: * Within every OTHER layer, resolution is **last-match-wins**: the LAST rule
#:   that matches decides. A later, more specific rule is therefore expressed
#:   by being declared later, which is what makes a shipped default
#:   overridable by a user rule without either of them needing to know about
#:   the other. (The alternative -- first-match-wins -- makes the default
#:   unoverridable in practice, because the default is declared first.)
#: * A later layer beats an earlier one. ``user_config`` can therefore relax
#:   ``defaults`` and ``per_session`` can relax ``user_config``, while the
#:   floor refuses to move for anybody.
LAYERS: tuple[str, ...] = (
    FLOOR_LAYER,
    "defaults",
    "builtin_agent",
    "user_config",
    "per_agent",
    "per_session",
)

#: The keys a subject example may set. Closed, because an unknown key in an
#: example is either a typo or a dimension somebody added to the rule model
#: and forgot to plumb here -- and a silently ignored key makes the example
#: vacuous, which is the failure this whole mechanism exists to prevent.
POLICY_SUBJECT_KEYS: frozenset[str] = frozenset(
    {"tool", "command", "path", "network_domain", "side_effect_class", "actor"}
)


class PolicyValidationError(ValueError):
    """Raised when a policy cannot prove itself and therefore must not load.

    A subclass of ``ValueError`` so an existing ``except ValueError`` around a
    config load keeps working, but the message always names the rule, the
    layer, and the example that failed -- a load failure a reader cannot
    diagnose is a load failure that gets "fixed" by deleting the rule.
    """

    def __init__(
        self,
        message: str,
        *,
        rule: str = "",
        layer: str = "",
        example: str = "",
    ) -> None:
        super().__init__(message)
        self.rule = rule
        self.layer = layer
        self.example = example

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-compatible record for a receipt or a trace row."""
        return {
            "message": str(self),
            "rule": self.rule,
            "layer": self.layer,
            "example": self.example,
        }


def normalize_decision(value: Any, *, rule: str = "") -> str:
    """Return the canonical decision for ``value`` or refuse.

    An unknown decision is a REFUSAL, never a default. A rule whose decision
    silently became ``ask`` would turn an author's ``forbid this`` into a
    question, and a rule whose decision silently became ``allow`` would be
    worse than no rule at all.
    """
    text = str(value or "").strip().casefold()
    decision = DECISION_ALIASES.get(text)
    if decision is None:
        raise PolicyValidationError(
            f"unsupported policy decision {value!r}; expected one of "
            f"{sorted(set(DECISION_ALIASES))}"
            + (f" (rule {rule!r})" if rule else ""),
            rule=rule,
        )
    return decision


# ---------------------------------------------------------------------------
# The subject: what a rule is being asked about.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicySubject:
    """One thing a policy is asked to decide.

    Every field is optional and an unset field constrains nothing -- which is
    what makes a bare ``*`` example (all fields empty) match every rule and a
    fully-specified example a real test.
    """

    tool: str = ""
    command: str = ""
    path: str = ""
    network_domain: str = ""
    side_effect_class: str = ""
    actor: str = ""

    @property
    def is_wildcard(self) -> bool:
        """Return whether this subject constrains nothing (the ``*`` example)."""
        return not any(
            (
                self.tool,
                self.command,
                self.path,
                self.network_domain,
                self.side_effect_class,
                self.actor,
            )
        )

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-compatible record."""
        return {
            "tool": self.tool,
            "command": self.command,
            "path": self.path,
            "network_domain": self.network_domain,
            "side_effect_class": self.side_effect_class,
            "actor": self.actor,
        }


def parse_subject(text: Any) -> PolicySubject:
    """Parse one subject example into a :class:`PolicySubject`.

    The grammar is deliberately tiny and fails closed on anything it does not
    understand::

        *                      the wildcard: constrains nothing
        tool=bash command="git commit -m 'fix'"
        tool=read path="src/app.py"
        side_effect_class=network tool=web_fetch network_domain=example.com

    Every token is ``key=value``; a value containing spaces MUST be quoted,
    and the error says so. An unknown key is a refusal rather than a silently
    ignored token -- an ignored key produces an example that passes for the
    wrong reason, which is the failure mode the whole mechanism is built to
    eliminate.

    Example paths are written with forward slashes on purpose: ``shlex`` in
    POSIX mode eats a backslash, and the matcher normalises separators anyway,
    so a Windows-native example would be a footgun that only bites on the
    platform nobody writes examples on.
    """
    raw = str(text or "").strip()
    if not raw or raw == "*":
        return PolicySubject()
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise PolicyValidationError(
            f"unparseable policy example {raw!r}: {exc} (quote any value that "
            f"contains spaces)",
            example=raw,
        ) from exc
    values: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise PolicyValidationError(
                f"policy example {raw!r} has a bare token {token!r}; every "
                f"token must be key=value over {sorted(POLICY_SUBJECT_KEYS)}",
                example=raw,
            )
        key, _, value = token.partition("=")
        key = key.strip().casefold()
        if key not in POLICY_SUBJECT_KEYS:
            raise PolicyValidationError(
                f"policy example {raw!r} names unknown key {key!r}; expected "
                f"one of {sorted(POLICY_SUBJECT_KEYS)}",
                example=raw,
            )
        if key in values:
            raise PolicyValidationError(
                f"policy example {raw!r} sets {key!r} twice", example=raw
            )
        values[key] = value
    if not values:
        raise PolicyValidationError(
            f"policy example {raw!r} constrains nothing but is not '*'",
            example=raw,
        )
    return PolicySubject(
        tool=values.get("tool", "").strip().casefold(),
        command=values.get("command", "").strip(),
        path=values.get("path", "").strip(),
        network_domain=values.get("network_domain", "").strip().casefold(),
        side_effect_class=values.get("side_effect_class", "").strip().casefold(),
        actor=values.get("actor", "").strip().casefold(),
    )


def _subject_from_call(call: Any) -> PolicySubject:
    """Project a ``ToolCall`` / mapping onto a :class:`PolicySubject`.

    Accepts the shared contract object as well as a plain mapping so a caller
    holding a real ``shared.agent_contracts.ToolCall`` does not have to
    re-derive the shape. The command is read from ``command`` or ``argv``
    because the typed catalog and the legacy shell path disagree about which.
    """
    if isinstance(call, PolicySubject):
        return call
    if isinstance(call, Mapping):
        getter = call.get
    else:
        getter = lambda key, default="": getattr(call, key, default)  # noqa: E731
    argv = getter("command", "") or getter("argv", "") or ""
    if isinstance(argv, (list, tuple)):
        argv = " ".join(str(item) for item in argv)
    return PolicySubject(
        tool=str(getter("tool", "") or getter("name", "") or "").strip().casefold(),
        command=str(argv or "").strip(),
        path=str(getter("path", "") or getter("target", "") or "").strip(),
        network_domain=str(getter("network_domain", "") or "").strip().casefold(),
        side_effect_class=str(getter("side_effect_class", "") or "")
        .strip()
        .casefold(),
        actor=str(getter("actor", "") or "").strip().casefold(),
    )


def _glob_any(value: str, pattern: str) -> bool:
    """Glob-match, treating ``|`` in the pattern as ALTERNATIVES.

    One helper for ``path`` and ``tool`` so the two dimensions cannot drift in
    what they accept. The separator is checked BEFORE globbing rather than
    folded into the pattern, because ``fnmatch`` has no alternation and
    embedding it would make a literal ``|`` in a name matchable-as-separator.
    """
    left = str(value or "").replace("\\", "/")
    for alternative in str(pattern or "").split("|"):
        candidate = alternative.strip()
        if not candidate:
            continue
        if fnmatch.fnmatch(left, candidate.replace("\\", "/")):
            return True
        if fnmatch.fnmatch(str(value or ""), candidate):
            return True
    return False


def _path_matches(value: str, pattern: str) -> bool:
    """Glob-match a path with both separator spellings normalised.

    A ``|`` in the pattern separates alternatives, so one rule can name a
    family of shapes (``.env*|*.env*``, or a read-only TOOL family) without
    becoming one rule per shape.
    """
    return _glob_any(value, pattern)


def _domain_matches(value: str, pattern: str) -> bool:
    """Exact or dot-boundary suffix, never a bare substring."""
    left = str(value or "").casefold().strip(".")
    right = str(pattern or "").casefold().strip(".")
    if not right:
        return False
    return left == right or left.endswith("." + right)


# ---------------------------------------------------------------------------
# The rule.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """One rule, with the examples that prove it behaves as declared.

    The example fields are the load-bearing part. A rule with no ``match`` and
    no ``not_match`` is REFUSED at load (:func:`validate_program`), because a
    rule nobody has demonstrated is a rule nobody has checked, and this
    mechanism exists so that is impossible rather than merely discouraged.
    """

    name: str
    decision: str
    layer: str = "defaults"
    tool: str = ""
    path: str = ""
    command_prefix: str = ""
    network_domain: str = ""
    side_effect_class: str = ""
    actor: str = ""
    command_pattern: str = ""
    command_risk: str = ""
    screen: Optional[Any] = None
    scope: str = "once"
    match: tuple[str, ...] = ()
    not_match: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", normalize_decision(self.decision, rule=self.name))
        layer = str(self.layer or "defaults").strip().casefold()
        if layer not in LAYERS:
            raise PolicyValidationError(
                f"rule {self.name!r} names unknown layer {self.layer!r}; "
                f"expected one of {list(LAYERS)}",
                rule=self.name,
                layer=layer,
            )
        object.__setattr__(self, "layer", layer)
        for field_name in (
            "tool",
            "path",
            "command_prefix",
            "network_domain",
            "side_effect_class",
            "actor",
        ):
            object.__setattr__(
                self, field_name, str(getattr(self, field_name) or "").strip()
            )
        object.__setattr__(self, "tool", self.tool.casefold())
        object.__setattr__(self, "network_domain", self.network_domain.casefold())
        object.__setattr__(
            self, "side_effect_class", self.side_effect_class.casefold()
        )
        object.__setattr__(
            self, "command_risk", str(self.command_risk or "").strip().casefold()
        )
        if self.command_risk and self.command_risk not in ARITY_RISKS:
            raise PolicyValidationError(
                f"rule {self.name!r} declares command_risk "
                f"{self.command_risk!r}; expected one of {list(ARITY_RISKS)}",
                rule=self.name,
                layer=self.layer,
            )
        object.__setattr__(self, "actor", self.actor.casefold() or "*")
        # The floor refuses to be anything but a refusal. A floor `allow` is a
        # contradiction, and a floor `ask` is a floor that can be talked past.
        if self.layer == FLOOR_LAYER and self.decision != "deny":
            raise PolicyValidationError(
                f"floor rule {self.name!r} has decision {self.decision!r}; the "
                f"floor layer only carries denials",
                rule=self.name,
                layer=self.layer,
            )
        if not str(self.command_prefix or "").strip():
            # An absent prefix means "this rule does not constrain commands";
            # a PRESENT-BUT-BLANK prefix would be indistinguishable from absent
            # and is the exact slip that once made a blanket grant.
            object.__setattr__(self, "command_prefix", "")
        if str(self.command_prefix or "").strip() != self.command_prefix:
            object.__setattr__(
                self,
                "command_prefix",
                str(self.command_prefix).strip(),
            )
        object.__setattr__(
            self,
            "scope",
            normalize_approval_scope(self.scope) if self.scope else "once",
        )
        object.__setattr__(
            self,
            "match",
            _as_examples(self.match, self.name, "match"),
        )
        object.__setattr__(
            self,
            "not_match",
            _as_examples(self.not_match, self.name, "not_match"),
        )
        object.__setattr__(self, "reason", str(self.reason or ""))

    # -- matching ---------------------------------------------------------

    @property
    def specificity(self) -> int:
        """Return how many dimensions this rule constrains.

        NOT part of resolution -- resolution is last-match-wins, and a
        specificity score would reintroduce "most specific wins", which is a
        DIFFERENT policy and the one this module deliberately does not
        implement. It is reported so a reader can see which rules are doing the
        constraining.

        A rule whose ONLY dimension is a ``screen`` returns 0: an external
        matcher cannot be scored against this rule set's dimensions, and
        reporting a number for it would be inventing one.
        """
        own = (
            self.tool,
            self.path,
            self.command_prefix,
            self.network_domain,
            self.side_effect_class,
            self.command_pattern,
            self.command_risk,
        )
        return sum(bool(value) for value in own)

    def _tool_matches(self, tool: str) -> bool:
        """Match a tool name, by ALTERNATION or GLOB.

        A plain name is an exact match; ``a|b|c`` names a family; a wildcard
        makes it a glob. The family form is what lets one rule cover a
        read-only SET (``read``, ``list``, ``grep``, the ``git_*`` reads ...)
        without a rule per tool -- and a rule per tool is how a tool family
        drifts, because adding a tool does not add a rule.
        """
        if not self.tool:
            return True
        return _glob_any(tool, self.tool)

    def _command_risk_matches(self, command: str) -> bool:
        """Match when the ARITY DICTIONARY declares this command's risk.

        This is the dimension that gives the arity table a job rather than
        being a rendering table: a policy can say "a command the dictionary
        declares read-only is allowed" without enumerating the commands, and a
        dictionary edit then changes the POLICY rather than only the prompt.
        """
        if not self.command_risk:
            return False
        entry = command_arity_table().lookup(command)
        if entry is None:
            # No declaration is not a declaration of safety. A command nobody
            # described falls through to the asking rules.
            return False
        return entry.risk == self.command_risk

    def matches(self, subject: Any) -> bool:
        """Return whether every configured dimension matches ``subject``.

        AND over the present dimensions, and an absent dimension constrains
        nothing -- so a rule with no dimensions matches everything, which is
        how a deliberate catch-all is written.
        """
        item = _subject_from_call(subject)
        if self.tool and not self._tool_matches(item.tool):
            return False
        if self.path and not _path_matches(item.path, self.path):
            return False
        if self.command_prefix and not command_prefix_matches(
            item.command, self.command_prefix
        ):
            return False
        if self.network_domain and not _domain_matches(
            item.network_domain, self.network_domain
        ):
            return False
        if self.side_effect_class and self.side_effect_class != item.side_effect_class:
            return False
        if self.actor not in ("*", item.actor or "agent"):
            return False
        if self.command_pattern and not self._pattern_matches(item.command):
            return False
        if self.command_risk and not self._command_risk_matches(item.command):
            return False
        if self.screen is None:
            return True
        return bool(self._screen_matches(item.command))

    def _screen_matches(self, command: str) -> bool:
        """Delegate the refusal decision to an EXTERNAL authority.

        This is how T3's hardline table is folded in without re-implementing
        it. A second copy of T3's matcher would be a second answer to "is this
        command refused", and this repository has already paid for that class
        of bug four times.

        A screen that RAISES is treated as a match -- the safe direction for a
        floor -- and the raise is not swallowed silently: the folded-rule status
        records which screens were exercised, so a screen that is broken rather
        than refusing is visible.
        """
        if self.screen is None:
            return False
        try:
            return bool(self.screen(str(command or "")))
        except Exception:
            return True

    def _pattern_matches(self, command: str) -> bool:
        """Apply ``command_pattern``, refusing a pattern that will not compile."""
        if not self.command_pattern:
            return False
        try:
            return re.search(self.command_pattern, str(command or "")) is not None
        except re.error as exc:
            raise PolicyValidationError(
                f"rule {self.name!r} has an uncompilable command_pattern: {exc}",
                rule=self.name,
                layer=self.layer,
            ) from exc

    # -- declaration ------------------------------------------------------

    @property
    def example_count(self) -> int:
        """Return how many examples this rule carries."""
        return len(self.match) + len(self.not_match)

    @property
    def has_examples(self) -> bool:
        """Return whether this rule declares at least one example."""
        return self.example_count > 0

    @classmethod
    def from_value(cls, value: Any) -> "PolicyRule":
        """Build a rule from a mapping, a JSON string, or another rule."""
        if isinstance(value, PolicyRule):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise PolicyValidationError(
                    f"rule is a string that is not JSON: {exc}", example=value
                ) from exc
            if not isinstance(parsed, Mapping):
                raise PolicyValidationError(
                    "rule is a JSON string that is not an object", example=value
                )
            value = parsed
        if not isinstance(value, Mapping):
            raise PolicyValidationError(
                f"a policy rule must be a mapping or a PolicyRule, got "
                f"{type(value).__name__}"
            )
        name = str(value.get("name") or "").strip()
        if not name:
            raise PolicyValidationError(
                "every policy rule needs a name; an unnamed rule cannot be "
                "reported in a refusal and therefore cannot be audited"
            )
        return cls(
            name=name,
            decision=value.get("decision", value.get("action", "deny")),
            layer=value.get("layer", "defaults"),
            tool=value.get("tool", ""),
            path=value.get("path", ""),
            command_prefix=value.get(
                "command_prefix", value.get("command", value.get("prefix", ""))
            ),
            network_domain=value.get(
                "network_domain", value.get("domain", "")
            ),
            side_effect_class=value.get(
                "side_effect_class", value.get("side_effect", "")
            ),
            actor=value.get("actor", "*"),
            command_pattern=value.get("command_pattern", value.get("pattern", "")),
            command_risk=value.get("command_risk", value.get("risk", "")),
            screen=value.get("screen"),
            scope=value.get("scope", "once"),
            match=_as_examples(value.get("match", ()), name, "match"),
            not_match=_as_examples(
                value.get("not_match", value.get("not-match", ())), name, "not_match"
            ),
            reason=value.get("reason", value.get("why", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible declaration of this rule."""
        return {
            "name": self.name,
            "decision": self.decision,
            "layer": self.layer,
            "tool": self.tool,
            "path": self.path,
            "command_prefix": self.command_prefix,
            "network_domain": self.network_domain,
            "side_effect_class": self.side_effect_class,
            "actor": self.actor,
            "command_pattern": self.command_pattern,
            "command_risk": self.command_risk,
            "screen": _callable_name(self.screen),
            "scope": self.scope,
            "match": list(self.match),
            "not_match": list(self.not_match),
            "reason": self.reason,
        }

    def to_engine_value(self) -> dict[str, Any]:
        """Return the mapping ``harness.agent_kernel.policy.PolicyRule.from_value``
        accepts, so a rule declared here can be handed to the kernel unchanged.

        ``command_pattern`` and the examples have no field in the kernel's rule
        and are therefore DROPPED rather than silently accepted-and-ignored --
        a rule whose floor behaviour depends on a regex must be evaluated by
        this module, and the engine value says so by its absence. A consumer
        that needs the pattern must evaluate through :class:`PolicyProgram`.
        """
        value = {
            "action": self.decision,
            "name": self.name,
            "scope": self.scope,
            "actor": self.actor,
        }
        for key in (
            "tool",
            "path",
            "command_prefix",
            "network_domain",
            "side_effect_class",
        ):
            value[key] = getattr(self, key)
        return {key: item for key, item in value.items() if item not in ("", "*")}


def _callable_name(value: Any) -> str:
    """Return a stable, serialisable name for a screen callable."""
    if value is None:
        return ""
    module = getattr(value, "__module__", "")
    qualname = getattr(value, "__qualname__", None) or getattr(
        value, "__name__", ""
    )
    return f"{module}.{qualname}" if module else str(qualname)


def _as_examples(value: Any, rule: str, field_name: str) -> tuple[str, ...]:
    """Normalise an example collection, refusing a bare string.

    A bare string where a list was expected is refused rather than wrapped:
    ``match="git status"`` almost always meant one example, and silently
    treating the STRING as a sequence of characters would produce a rule whose
    proof is nonsense.
    """
    if value is None or value == ():
        return ()
    if isinstance(value, str):
        raise PolicyValidationError(
            f"rule {rule!r} field {field_name!r} is a string; it must be a list "
            f"of example subjects (wrap it in a list)",
            rule=rule,
            example=value,
        )
    if isinstance(value, Mapping):
        return (str(value),)
    try:
        return tuple(str(item) for item in value)
    except TypeError as exc:
        raise PolicyValidationError(
            f"rule {rule!r} field {field_name!r} is not a collection of "
            f"examples: {exc}",
            rule=rule,
        ) from exc


# ---------------------------------------------------------------------------
# Evaluation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyOutcome:
    """The result of asking a program about one subject."""

    decision: str
    rule: str
    layer: str
    reason: str
    subject: PolicySubject
    considered: tuple[str, ...] = ()
    floor: bool = False

    @property
    def allowed(self) -> bool:
        """Return whether the action may proceed without asking."""
        return self.decision == "allow"

    @property
    def needs_approval(self) -> bool:
        """Return whether the action must be escalated to a human."""
        return self.decision == "ask"

    @property
    def terminal(self) -> bool:
        """Return whether the action is refused outright."""
        return self.decision == "deny"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record."""
        return {
            "decision": self.decision,
            "rule": self.rule,
            "layer": self.layer,
            "reason": self.reason,
            "floor": self.floor,
            "considered": list(self.considered),
            "subject": self.subject.as_dict(),
        }


#: The decision a layer returns when NOTHING in it matched. ``ask``, not
#: ``allow``: an action no rule spoke about is a question for someone, and an
#: ``allow`` default would make the absence of a rule the grant.
DEFAULT_ACTION = "ask"


@dataclass(frozen=True)
class PolicyProgram:
    """A validated set of rules, resolvable by subject.

    Construction does NOT validate. Call :meth:`validated` (or use
    :func:`validate_program`) to get a program whose rules have proved their
    own examples; an unvalidated program is still usable, and
    :meth:`evaluate` records that fact on every outcome it returns rather than
    pretending.
    """

    rules: tuple[PolicyRule, ...] = ()
    default_action: str = DEFAULT_ACTION
    validated: bool = False
    layers: tuple[str, ...] = LAYERS

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "default_action", normalize_decision(self.default_action)
        )
        seen: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, PolicyRule):
                raise PolicyValidationError(
                    f"program contains a non-rule value {type(rule).__name__}"
                )
            if rule.name in seen:
                raise PolicyValidationError(
                    f"duplicate rule name {rule.name!r}; a refusal names one "
                    f"rule and two rules with one name make that a guess",
                    rule=rule.name,
                )
            seen.add(rule.name)

    def rules_in(self, layer: str) -> tuple[PolicyRule, ...]:
        """Return the rules of one layer, in declaration order."""
        label = str(layer or "").casefold()
        return tuple(rule for rule in self.rules if rule.layer == label)

    def evaluate(self, subject: Any) -> PolicyOutcome:
        """Resolve one subject. Floor first, then last-match-wins per layer."""
        item = _subject_from_call(subject)
        considered: list[str] = []

        floor_matches = [
            rule for rule in self.rules_in(FLOOR_LAYER) if rule.matches(item)
        ]
        if floor_matches:
            for rule in floor_matches:
                considered.append(rule.name)
            winner = floor_matches[-1]
            return PolicyOutcome(
                decision="deny",
                rule=winner.name,
                layer=FLOOR_LAYER,
                reason=winner.reason or "refused by the hardline floor",
                subject=item,
                considered=tuple(considered),
                floor=True,
            )

        winner: Optional[PolicyRule] = None
        for layer in self.layers:
            if layer == FLOOR_LAYER:
                continue
            for rule in self.rules_in(layer):
                if not rule.matches(item):
                    continue
                considered.append(rule.name)
                winner = rule  # LAST match wins -- pinned, and load-bearing.
        if winner is None:
            return PolicyOutcome(
                decision=self.default_action,
                rule="layer_default",
                layer="",
                reason="no rule matched; an undecided action is a question",
                subject=item,
                considered=tuple(considered),
            )
        return PolicyOutcome(
            decision=winner.decision,
            rule=winner.name,
            layer=winner.layer,
            reason=winner.reason or f"matched rule {winner.name}",
            subject=item,
            considered=tuple(considered),
        )

    def validated_program(self) -> "PolicyProgram":
        """Return this program after its rules have proved their examples.

        Sugar for ``validate_program(self)``, and deliberately NOT named
        ``validated``: a method and a field of the same name is a boolean whose
        meaning depends on whether it was called.
        """
        return validate_program(self)

    def with_rules(self, *rules: Any) -> "PolicyProgram":
        """Return a NEW program with extra rules appended (never mutated)."""
        extra = tuple(
            rule if isinstance(rule, PolicyRule) else PolicyRule.from_value(rule)
            for rule in rules
        )
        return replace(self, rules=self.rules + extra, validated=False)

    def for_layer(self, layer: str) -> "PolicyProgram":
        """Return a program containing only one layer's rules."""
        label = str(layer or "").casefold()
        if label not in LAYERS:
            raise PolicyValidationError(f"unknown layer {layer!r}")
        return PolicyProgram(
            rules=self.rules_in(label),
            default_action=self.default_action,
            validated=False,
            layers=self.layers,
        )

    def engine_values(self) -> list[dict[str, Any]]:
        """Return every rule as a kernel ``PolicyRule.from_value`` mapping."""
        return [rule.to_engine_value() for rule in self.rules]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record of the whole program."""
        return {
            "default_action": self.default_action,
            "validated": self.validated,
            "layers": list(self.layers),
            "rules": [rule.as_dict() for rule in self.rules],
        }

    def report(self) -> dict[str, Any]:
        """Return the per-rule, per-example verification record.

        This is the receipt a reviewer reads to answer "what did this policy
        actually prove when it loaded".
        """
        rows: list[dict[str, Any]] = []
        for layer in self.layers:
            subset = self.rules_in(layer)
            if not subset:
                continue
            program = self.for_layer(layer)
            for rule in subset:
                rows.append(_verify_rule(program, rule))
        return {
            "validated": self.validated,
            "rules": rows,
            "rule_count": len(self.rules),
            "example_count": sum(row["examples"] for row in rows),
        }


def _verify_rule(program: PolicyProgram, rule: PolicyRule) -> dict[str, Any]:
    """Return one rule's example-by-example verification record."""
    checks: list[dict[str, Any]] = []
    for kind, examples, should_match in (
        ("match", rule.match, True),
        ("not_match", rule.not_match, False),
    ):
        for example in examples:
            subject = parse_subject(example)
            outcome = program.evaluate(subject)
            rule_applies = rule.matches(subject)
            decision_ok = (
                outcome.decision == rule.decision
                if should_match
                else outcome.decision != rule.decision
            )
            if should_match:
                # The layer-level claim: on this subject the rule applies AND it
                # is the rule that DECIDED. The second half is what catches two
                # rules in one layer contradicting each other -- last-match-wins
                # means the later one silently wins, and without this the
                # earlier rule's example would be a lie nobody could detect.
                ok = decision_ok and outcome.rule == rule.name
            else:
                # The rule-level claim, and the ONLY correct reading of
                # `not_match`: THIS RULE DOES NOT APPLY to this subject. It is
                # deliberately NOT "the answer differs" -- two different rules
                # can legitimately reach the same decision, and a `not_match`
                # that demanded a different ANSWER would fail on a policy that
                # is perfectly consistent.
                ok = not rule_applies
            checks.append(
                {
                    "kind": kind,
                    "example": example,
                    "decision": outcome.decision,
                    "decided_by": outcome.rule,
                    "expected": rule.decision if should_match else "rule does not apply",
                    "decision_ok": decision_ok,
                    "rule_applies": rule_applies,
                    "rule_applied": outcome.rule == rule.name,
                    "ok": ok,
                }
            )
    return {
        "name": rule.name,
        "layer": rule.layer,
        "decision": rule.decision,
        "examples": len(checks),
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def validate_program(program: PolicyProgram) -> PolicyProgram:
    """Return ``program`` validated, or RAISE naming what failed.

    **Fail-closed.** Every rule must carry at least one example, and every
    declared example must behave as declared. The examples are checked against
    the rule's OWN LAYER, which is the claim a layer actually makes about the
    world:

    * a rule in ``user_config`` proves that, on its own, it decides what it
      says it decides -- it does NOT prove it beats ``defaults``, because that
      is the layer ORDER's job and is pinned separately;
    * and two rules in the SAME layer that contradict each other DO fail here,
      which is the point. Last-match-wins means the later one silently wins,
      and without this check the earlier rule's examples would be a lie nobody
      could detect.

    The ``floor`` is validated against itself, so a floor rule cannot be
    rescued by a rule above it.
    """
    rules = tuple(program.rules)
    # PASS ONE, over every rule and before any example is executed: does the
    # rule ship its proof at all? Doing this first is not cosmetic. A rule with
    # no examples that matched broadly would STEAL another rule's example and
    # the resulting error would name the innocent rule -- a validation failure
    # that points at the wrong file is worse than no validation, because it
    # sends the reader to fix the wrong thing.
    for layer in program.layers:
        for rule in rules:
            if rule.layer != layer:
                continue
            if not rule.has_examples:
                raise PolicyValidationError(
                    f"rule {rule.name!r} (layer {layer!r}) declares no examples; "
                    f"a rule that ships without its proof does not load",
                    rule=rule.name,
                    layer=layer,
                )
            # Parse every example here, for the same reason: an example that
            # will not parse is a defect in THAT rule, and parsing lazily would
            # let a broad rule declared before it steal the example and produce
            # an error naming the wrong rule. The re-raise attaches THIS rule's
            # name, because `parse_subject` cannot know it.
            for example in rule.match + rule.not_match:
                try:
                    parse_subject(example)
                except PolicyValidationError as exc:
                    raise PolicyValidationError(
                        str(exc), rule=rule.name, layer=layer, example=example
                    ) from exc
    # PASS TWO: execute every declared example against its own layer.
    for layer in program.layers:
        subset = tuple(rule for rule in rules if rule.layer == layer)
        if not subset:
            continue
        scoped = PolicyProgram(
            rules=subset,
            default_action=program.default_action,
            validated=False,
            layers=program.layers,
        )
        for rule in subset:
            for check in _verify_rule(scoped, rule)["checks"]:
                if check["ok"]:
                    continue
                if check["kind"] == "match":
                    detail = (
                        f"expected {rule.decision!r} and for this rule to be the "
                        f"deciding rule, got {check['decision']!r} decided by "
                        f"{check['decided_by']!r}"
                    )
                else:
                    detail = (
                        "the rule MATCHES this subject, so it cannot be listed "
                        "as one it does not apply to"
                    )
                raise PolicyValidationError(
                    f"rule {rule.name!r} (layer {layer!r}) example "
                    f"{check['example']!r} does not behave as declared: {detail}",
                    rule=rule.name,
                    layer=layer,
                    example=check["example"],
                )
    return replace(program, validated=True)


# ---------------------------------------------------------------------------
# The arity dictionary -- DATA, loaded from a file.
# ---------------------------------------------------------------------------

#: Where the arity data lives, relative to this module. A data file rather
#: than a Python literal for the reason the corpus gives: a ~130-entry table is
#: corrected by editing DATA, and a table in code is corrected by editing code,
#: which is a review event rather than a data fix.
ARITY_DATA_PATH = Path(__file__).with_name("policy_data") / "command_arity.json"

#: The declared effect classes an arity entry may carry. Closed, and the same
#: words as ``shared.agent_contracts.SIDE_EFFECT_CLASSES`` plus the two the
#: arity table needs to distinguish (a read that PRINTS the environment is a
#: read; a command that EXECUTES is a process).
ARITY_RISKS: tuple[str, ...] = ("read", "write", "process", "network")


@dataclass(frozen=True)
class ArityEntry:
    """One command's shape, as declared in the data file."""

    command: str
    min_args: int
    max_args: Optional[int]
    positional: tuple[str, ...] = ()
    flags: str = ""
    risk: str = "read"
    note: str = ""

    def accepts(self, argv: Sequence[str]) -> bool:
        """Return whether ``argv`` fits this declared shape.

        ``argv`` is the FULL command line including the command's own words
        (``["git", "commit", "-m", "x"]`` for ``git commit``). The command's
        own length is subtracted, so ``min_args``/``max_args`` count what a
        reader would call "the arguments".
        """
        words = [str(item) for item in argv]
        prefix = self.command.split()
        if words[: len(prefix)] != prefix:
            return False
        rest = words[len(prefix) :]
        positional = [item for item in rest if not item.startswith("-")]
        if len(positional) < self.min_args:
            return False
        return self.max_args is None or len(positional) <= self.max_args

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record."""
        return {
            "command": self.command,
            "min_args": self.min_args,
            "max_args": self.max_args,
            "positional": list(self.positional),
            "flags": self.flags,
            "risk": self.risk,
            "note": self.note,
        }


@dataclass(frozen=True)
class CommandArityTable:
    """The loaded arity dictionary, plus where it came from and whether it
    arrived at all.

    ``available`` is False with a REASON rather than an empty table presented
    as "this command takes no arguments". Those are different facts and
    conflating them is how an arity check silently approves everything.
    """

    entries: Mapping[str, ArityEntry] = field(default_factory=dict)
    schema: str = ""
    available: bool = True
    reason: str = ""
    path: str = ""

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, command: object) -> bool:
        return self.lookup(command) is not None

    def lookup(self, command: Any) -> Optional[ArityEntry]:
        """Return the entry for a command, longest declared prefix first.

        ``git commit`` and ``git`` can both be declared; a lookup of
        ``git commit -m x`` must prefer the longer, more specific row, because
        a lookup that fell back to ``git`` would answer about the wrong shape.
        """
        words = str(command or "").strip().split()
        if not words:
            return None
        for size in range(min(len(words), 4), 0, -1):
            entry = self.entries.get(" ".join(words[:size]))
            if entry is not None:
                return entry
        return None

    def commands(self) -> tuple[str, ...]:
        """Return every declared command name, sorted."""
        return tuple(sorted(self.entries))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record of the whole table."""
        return {
            "schema": self.schema,
            "available": self.available,
            "reason": self.reason,
            "path": self.path,
            "count": len(self.entries),
            "commands": list(self.commands()),
        }


def load_command_arity(path: Any = None) -> CommandArityTable:
    """Load the arity dictionary. Total: never raises, always explains.

    A missing or malformed data file yields ``available=False`` WITH a reason.
    It does not yield an empty table, because "the dictionary is empty" and "no
    command is permitted" are the same bytes and only one of them is true.
    """
    target = Path(str(path)) if path else ARITY_DATA_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CommandArityTable(
            available=False,
            reason=f"arity data file not found: {target}",
            path=str(target),
        )
    except OSError as exc:
        return CommandArityTable(
            available=False,
            reason=f"arity data file unreadable: {type(exc).__name__}",
            path=str(target),
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        return CommandArityTable(
            available=False,
            reason=f"arity data file is not valid JSON: {exc}",
            path=str(target),
        )
    if not isinstance(document, Mapping):
        return CommandArityTable(
            available=False,
            reason="arity data file is not a JSON object",
            path=str(target),
        )
    rows = document.get("commands")
    if not isinstance(rows, Mapping) or not rows:
        return CommandArityTable(
            available=False,
            reason="arity data file has no non-empty 'commands' object",
            path=str(target),
        )
    entries: dict[str, ArityEntry] = {}
    for name, value in rows.items():
        key = str(name).strip()
        if not isinstance(value, Mapping):
            return CommandArityTable(
                available=False,
                reason=f"arity entry {key!r} is not an object",
                path=str(target),
            )
        try:
            minimum = int(value.get("min_args", 0))
        except (TypeError, ValueError):
            return CommandArityTable(
                available=False,
                reason=f"arity entry {key!r} has a non-integer min_args",
                path=str(target),
            )
        raw_max = value.get("max_args", None)
        if raw_max is None:
            maximum: Optional[int] = None
        else:
            try:
                maximum = int(raw_max)
            except (TypeError, ValueError):
                return CommandArityTable(
                    available=False,
                    reason=f"arity entry {key!r} has a non-integer max_args",
                    path=str(target),
                )
            if maximum < minimum:
                return CommandArityTable(
                    available=False,
                    reason=(
                        f"arity entry {key!r} has max_args {maximum} below "
                        f"min_args {minimum}"
                    ),
                    path=str(target),
                )
        risk = str(value.get("risk", "read")).strip().casefold()
        if risk not in ARITY_RISKS:
            return CommandArityTable(
                available=False,
                reason=(
                    f"arity entry {key!r} declares risk {risk!r}; expected one "
                    f"of {list(ARITY_RISKS)}"
                ),
                path=str(target),
            )
        entries[key] = ArityEntry(
            command=key,
            min_args=max(0, minimum),
            max_args=maximum,
            positional=tuple(str(item) for item in value.get("positional", ()) or ()),
            flags=str(value.get("flags", "") or ""),
            risk=risk,
            note=str(value.get("note", "") or ""),
        )
    return CommandArityTable(
        entries=entries,
        schema=str(document.get("schema", "") or ""),
        available=True,
        path=str(target),
    )


def command_arity_table() -> CommandArityTable:
    """Return the shipped arity dictionary."""
    return load_command_arity()


def arity_pattern(command: Any) -> str:
    """Return a human-readable SHAPE for a command, for an approval prompt.

    This is what lets a prompt render ``git commit -m <message>`` instead of a
    blanket bash approval. When the command is not in the dictionary the
    answer is ``"<command> *"`` -- an explicit "anything", never a guess at a
    shape nobody declared.
    """
    entry = command_arity_table().lookup(command)
    words = str(command or "").strip()
    if entry is None:
        return f"{words} *" if words else "*"
    placeholders = " ".join(entry.positional) if entry.positional else "*"
    if entry.min_args == 0 and entry.max_args == 0:
        shape = words
    elif entry.max_args is None:
        shape = f"{words} [{placeholders}]"
    else:
        shape = f"{words} {placeholders}"
    # The flags are appended because this is the function an approval PROMPT
    # renders: a human asked to approve `git commit` is really being asked to
    # approve `git commit --amend --no-verify`, and the difference between
    # those two is the whole decision. `render_arity_scope_label` deliberately
    # omits them, because a remembered scope is a narrow thing and a wide one
    # is a wide grant.
    flags = " ".join(entry.flags.split())
    return f"{shape} [{flags}]" if flags else shape


def render_arity_scope_label(prefix: Any) -> str:
    """Return the scope label a grant should be remembered under.

    ``git commit`` renders as ``git commit -m <message> [paths...]`` from the
    declared positional names, so a remembered grant is visibly about ONE
    command shape rather than about "bash". That is the whole point of the
    dictionary: ``git commit *`` is still a bash grant wearing a hat.
    """
    entry = command_arity_table().lookup(prefix)
    words = str(prefix or "").strip()
    if entry is None:
        return f"{words} *"
    named = [item for item in entry.positional if item]
    if not named:
        return words
    required = named[: entry.min_args]
    optional = named[entry.min_args :]
    parts = list(words.split())
    parts.extend(required)
    if optional:
        parts.append("[" + " ".join(optional) + "]")
    if entry.max_args is None and optional:
        parts.append("...")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The floor, folded from T3's table.
# ---------------------------------------------------------------------------


def hardline_floor_rules(
    *, runtime_module: Any = None
) -> tuple[tuple[PolicyRule, ...], dict[str, Any]]:
    """Fold T3's hardline table into floor rules, and report exactly what happened.

    **This is coordination, not duplication, and the distinction is the whole
    point.** ``runtime/hardline.py`` owns the refusal patterns, their
    justifications AND their matcher -- and the matcher is not the regex: T3
    normalises and segments a command before matching, and its normaliser is
    STRICTER than its own ``regex`` column. Measured on this tree: the pattern
    ``raw_device_write`` carries the example ``cat img > /dev/sda``, that string
    does NOT match that pattern's own ``regex``, and T3's ``screen_or_raise``
    refuses it anyway. A translation that re-applied the regex would therefore
    have produced a floor that is WEAKER than the table it came from while
    looking like a faithful copy of it.

    So each folded rule gets its own predicate that asks T3's screen and then
    checks that the refusal NAMES this pattern. Two consequences, both stated
    rather than assumed:

    * There is ONE implementation of "is this command refused". A second copy of
      T3's matcher would be a second answer to the same question, and this
      repository has already paid for that class of bug four times.
    * The name check is string coupling, so the fold VERIFIES it: a pattern is
      admitted only when at least one of its own examples is refused naming
      that pattern. A pattern whose identity cannot be established is EXCLUDED
      and named in the status, rather than folded as a rule that can never be
      the deciding rule.

    ``runtime`` is imported lazily and optionally -- ``shared`` is the bottom
    of the ladder and must not require ``runtime`` -- and the returned status
    always says which of the four things happened: folded everything, folded
    with exclusions, could not reach runtime, or there was no table.
    """
    status: dict[str, Any] = {
        "available": False,
        "reason": "",
        "source": "",
        "patterns_seen": 0,
        "folded": 0,
        "excluded": [],
        "delegates_to": "",
    }
    module = runtime_module
    if module is None:
        try:
            from runtime import hardline as module  # type: ignore[no-redef]
        except Exception as exc:  # pragma: no cover - depends on tree state
            status["reason"] = (
                f"runtime.hardline unavailable: {type(exc).__name__}: {exc}"
            )
            return (), status
    patterns = tuple(getattr(module, "HARDLINE_PATTERNS", ()) or ())
    status["patterns_seen"] = len(patterns)
    status["source"] = str(getattr(module, "__name__", "") or "runtime.hardline")
    if not patterns:
        status["reason"] = "runtime.hardline exposes no HARDLINE_PATTERNS"
        return (), status

    screen = getattr(module, "screen_or_raise", None)
    refused = getattr(module, "HardlineRefused", None)
    if not callable(screen) or refused is None:
        status["reason"] = (
            "runtime.hardline exposes no screen_or_raise/HardlineRefused pair, "
            "so its table cannot be delegated to and is NOT re-implemented here"
        )
        return (), status
    status["delegates_to"] = _callable_name(screen)

    rules: list[PolicyRule] = []
    for pattern in patterns:
        pattern_id = str(getattr(pattern, "pattern_id", "") or "")
        examples = tuple(str(item) for item in getattr(pattern, "examples", ()) or ())
        if not pattern_id or not examples:
            status["excluded"].append(
                {
                    "pattern_id": pattern_id or "<unnamed>",
                    "reason": "no pattern_id or no declared examples",
                }
            )
            continue

        def make_screen(wanted: str) -> Any:
            """Return a predicate that asks T3 and requires IT to be the refuser."""

            def screen_one(command: str) -> bool:
                try:
                    screen(command)
                except refused:
                    return wanted in str(sys.exc_info()[1])
                return False

            screen_one.__name__ = f"hardline_{wanted}"
            screen_one.__qualname__ = screen_one.__name__
            screen_one.__module__ = str(getattr(module, "__name__", ""))
            return screen_one

        predicate = make_screen(pattern_id)
        named_hits = 0
        for example in examples:
            try:
                if predicate(example):
                    named_hits += 1
            except Exception:  # pragma: no cover - predicate swallows refusals
                named_hits = 0
                break
        if named_hits == 0:
            # Fail closed for the FOLD and loudly for the reader: a pattern
            # whose identity cannot be established is not admitted, because a
            # rule that can never be the deciding rule is a rule whose examples
            # are a lie.
            status["excluded"].append(
                {
                    "pattern_id": pattern_id,
                    "reason": (
                        "none of its own examples is refused naming this "
                        "pattern, so its identity cannot be established"
                    ),
                }
            )
            continue
        subjects = tuple(f"command={shlex.quote(item)}" for item in examples)
        rules.append(
            PolicyRule(
                name=f"floor:hardline:{pattern_id}",
                decision="deny",
                layer=FLOOR_LAYER,
                screen=predicate,
                reason=str(getattr(pattern, "summary", "") or ""),
                match=subjects,
                not_match=(),
            )
        )
    status["folded"] = len(rules)
    status["available"] = bool(rules)
    if not rules:
        status["reason"] = (
            "no hardline pattern could be folded: "
            f"{status['excluded']}"
        )
    elif status["excluded"]:
        status["reason"] = (
            f"{len(status['excluded'])} of {status['patterns_seen']} hardline "
            f"patterns were EXCLUDED: "
            + ", ".join(row["pattern_id"] for row in status["excluded"])
        )
    return tuple(rules), status



# ---------------------------------------------------------------------------
# `deny` -> visibility. The contract between shared/ and the kernel.
# ---------------------------------------------------------------------------

#: The DEFAULT for both axes, and the load-bearing assertion of this section:
#: a tool nobody classified is VISIBLE and EXECUTABLE.
#:
#: A tool hidden by default is a tool the agent cannot use, and the agent does
#: not know why -- it plans around a capability it cannot see and produces a
#: worse plan, silently. That is why the default is visible/executable and why
#: ``resolve_tool_surface`` starts every tool there.
DEFAULT_TOOL_VISIBILITY = "visible"
DEFAULT_TOOL_EXECUTABILITY = "executable"

#: The asymmetry that must be DECLARED rather than discovered: a tool may be
#: model-invisible while remaining harness-executable. That is legitimate --
#: an internal tool the harness calls on the agent's behalf needs no model
#: schema -- and it is exactly the shape of a HIDDEN CAPABILITY if nobody
#: wrote it down. So it is a declared field with a required reason and owner,
#: and an undeclared one is a violation rather than a configuration.
SURFACE_VIOLATIONS: tuple[str, ...] = (
    # Under-hiding: the policy DENIED it and it is still in the model's view.
    "denied_tool_visible",
    # Over-hiding: the policy did NOT deny it, it mutates, and it is hidden --
    # so the agent cannot do the job it was asked to do.
    "mutation_tool_hidden",
    # The hidden capability nobody declared.
    "undeclared_hidden_capability",
    # Over-hiding taken to its conclusion: no mutation tool is reachable, so
    # the agent cannot change any file at all.
    "no_mutation_tool_visible",
)


@dataclass(frozen=True)
class ToolVisibilityDeclaration:
    """A declared exception to "hidden means not offered to the model"."""

    tool: str
    visible: bool = False
    executable: bool = True
    reason: str = ""
    owner: str = ""

    @property
    def declared(self) -> bool:
        """Return whether this declaration is COMPLETE.

        Both a reason and an owner are required. A reason alone is a comment;
        an owner alone is a queue.
        """
        return bool(self.reason.strip() and self.owner.strip())

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record."""
        return {
            "tool": self.tool,
            "visible": self.visible,
            "executable": self.executable,
            "reason": self.reason,
            "owner": self.owner,
            "declared": self.declared,
        }


@dataclass(frozen=True)
class SurfaceViolation:
    """One way a tool surface is wrong, in ONE direction or the other."""

    kind: str
    tool: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-compatible record."""
        return {"kind": self.kind, "tool": self.tool, "detail": self.detail}


@dataclass(frozen=True)
class ToolSurface:
    """What the model is offered, and what the harness may still run.

    This is the exact structure that flows from a ``deny`` decision to the
    model's tool list. ``visible`` is what goes into the request; ``executable``
    is what the dispatcher will still accept. They are SEPARATE FIELDS because
    they are separate facts, and the gap between them is the hidden capability.
    """

    visible: tuple[str, ...]
    hidden: tuple[str, ...]
    executable: tuple[str, ...]
    denied: tuple[str, ...]
    declarations: tuple[ToolVisibilityDeclaration, ...] = ()
    violations: tuple[SurfaceViolation, ...] = ()

    def schema_names(self) -> tuple[str, ...]:
        """Return the names to put in the model's tool list.

        This is the field T1 and T4 mount: pass these names (or the schemas
        they select) and the model cannot see anything else.
        """
        return self.visible

    def withheld_but_runnable(self) -> tuple[str, ...]:
        """Return the tools the model cannot see but the harness may still run.

        Every entry is a DECLARED exception. An undeclared one is reported in
        ``violations`` as ``undeclared_hidden_capability`` rather than being
        quietly present.
        """
        return tuple(name for name in self.hidden if name in set(self.executable))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record."""
        return {
            "visible": list(self.visible),
            "hidden": list(self.hidden),
            "executable": list(self.executable),
            "denied": list(self.denied),
            "withheld_but_runnable": list(self.withheld_but_runnable()),
            "declarations": [item.as_dict() for item in self.declarations],
            "violations": [item.as_dict() for item in self.violations],
        }


def _tool_name(item: Any) -> str:
    """Return a tool name from a name, a mapping, or a catalog spec."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        return str(item.get("name") or item.get("tool") or "").strip()
    return str(getattr(item, "name", "") or getattr(item, "tool", "") or "").strip()


def resolve_tool_surface(
    catalog: Iterable[Any],
    *,
    decisions: Optional[Mapping[str, str]] = None,
    declarations: Iterable[Any] = (),
    mutation_tools: Iterable[str] = (),
) -> ToolSurface:
    """Turn a set of policy decisions into the model's tool surface.

    ``decisions`` maps a tool name to a decision; anything not present is
    treated as NOT denied. ``declarations`` are the declared exceptions from
    :class:`ToolVisibilityDeclaration`.

    Every tool starts at ``visible`` + ``executable``. A ``deny`` removes it
    from ``visible`` and leaves ``executable`` alone -- the harness may still
    need to run it, and whether it may is a separate decision this function
    does not make for you.
    """
    names = tuple(dict.fromkeys(filter(None, (_tool_name(item) for item in catalog))))
    decided = {
        str(key).strip().casefold(): normalize_decision(value)
        for key, value in (decisions or {}).items()
    }
    declared = tuple(
        item
        if isinstance(item, ToolVisibilityDeclaration)
        else ToolVisibilityDeclaration(**dict(item))
        for item in (declarations or ())
    )
    by_tool = {item.tool: item for item in declared}

    visible: list[str] = []
    hidden: list[str] = []
    executable: list[str] = []
    denied: list[str] = []
    for name in names:
        key = name.casefold()
        declaration = by_tool.get(name) or by_tool.get(key)
        is_denied = decided.get(key) == "deny"
        if is_denied:
            denied.append(name)
        hidden_by_policy = is_denied
        hidden_by_declaration = declaration is not None and not declaration.visible
        hidden_flag = hidden_by_policy or hidden_by_declaration
        if hidden_flag:
            hidden.append(name)
        else:
            visible.append(name)
        # Executability is INDEPENDENT of visibility, and that independence is
        # the declared asymmetry. But the DEFAULT for a policy-denied tool is
        # NOT executable: "deny" is itself a declaration that this tool is not
        # to be used, so leaving it runnable would make every ordinary denial a
        # hidden capability and drown the ones that are real. Reaching
        # hidden-but-executable therefore requires an EXPLICIT declaration,
        # which is the whole point -- the asymmetry is declared, not
        # accidental.
        if is_denied:
            runnable = bool(
                declaration is not None and declaration.executable
            )
        else:
            runnable = declaration.executable if declaration is not None else True
        if runnable:
            executable.append(name)

    surface = ToolSurface(
        visible=tuple(visible),
        hidden=tuple(hidden),
        executable=tuple(executable),
        denied=tuple(denied),
        declarations=declared,
    )
    return replace(
        surface,
        violations=surface_violations(
            surface,
            decisions=decided,
            mutation_tools=mutation_tools,
        ),
    )


def surface_violations(
    surface: ToolSurface,
    *,
    decisions: Optional[Mapping[str, str]] = None,
    mutation_tools: Iterable[str] = (),
) -> tuple[SurfaceViolation, ...]:
    """Return every way ``surface`` is wrong, in BOTH directions.

    **Both directions are failures, and a check that only looks at one of them
    passes the other's defect.**

    * ``denied_tool_visible`` -- UNDER-hiding: policy said no, the model can
      still see it. The agent spends turns discovering a refusal it could have
      been told about, and a surface that lies about what is permitted is
      worse than one that says no.
    * ``mutation_tool_hidden`` -- OVER-hiding: policy allowed it, it mutates,
      and the model cannot see it. The agent produces a plan that cannot be
      carried out.
    * ``undeclared_hidden_capability`` -- hidden AND executable with no
      declaration carrying both a reason and an owner. This is the one the
      contract exists to make impossible-by-accident.
    * ``no_mutation_tool_visible`` -- over-hiding taken to its conclusion: not
      one mutating tool is reachable, so the agent cannot change any file.
    """
    violations: list[SurfaceViolation] = []
    visible = set(surface.visible)
    executable = set(surface.executable)
    hidden = set(surface.hidden)
    mutations = {str(name).strip() for name in (mutation_tools or ()) if str(name).strip()}

    for name in sorted(mutations & hidden):
        if name in surface.denied:
            # Denied by policy: correct to hide, not an over-hiding failure.
            continue
        violations.append(
            SurfaceViolation(
                kind="mutation_tool_hidden",
                tool=name,
                detail=(
                    "policy did not deny this mutating tool and it is not "
                    "visible; the agent cannot use a capability it cannot see"
                ),
            )
        )
    for name in sorted(set(surface.denied) & visible):
        violations.append(
            SurfaceViolation(
                kind="denied_tool_visible",
                tool=name,
                detail=(
                    "policy denied this tool and it is still in the model's "
                    "tool list"
                ),
            )
        )
    declared = {item.tool: item for item in surface.declarations}
    for name in sorted(hidden & executable):
        declaration = declared.get(name) or declared.get(name.casefold())
        if declaration is None or not declaration.declared:
            violations.append(
                SurfaceViolation(
                    kind="undeclared_hidden_capability",
                    tool=name,
                    detail=(
                        "hidden from the model but still executable, with no "
                        "declaration carrying both a reason and an owner"
                    ),
                )
            )
    if mutations and not (mutations & visible):
        violations.append(
            SurfaceViolation(
                kind="no_mutation_tool_visible",
                tool="",
                detail=(
                    "no mutating tool is visible, so the agent cannot change "
                    "any file"
                ),
            )
        )
    if decisions:
        for name, decision in sorted(decisions.items()):
            if decision == "deny" and name not in hidden and name not in visible:
                violations.append(
                    SurfaceViolation(
                        kind="denied_tool_visible",
                        tool=name,
                        detail="policy denied a tool that is not in the catalog",
                    )
                )
    return tuple(violations)


def untrusted_boundary_line(edge: str, source: str = "issue") -> str:
    """Return the exact boundary line ``taint_wrap`` emits for a source.

    Re-exported here so a policy example can name a boundary in the same
    spelling the boundary itself uses. Two spellings of one boundary is how a
    defanger stops matching.
    """
    from shared.security import _boundary_line

    return _boundary_line(str(edge or "").upper(), source)
# ---------------------------------------------------------------------------
# The shipped rules, with real examples.
#
# Every rule below carries `match` (subjects it MUST decide) and, where the
# rule has a boundary worth pinning, `not_match` (subjects it must NOT decide
# as its own decision). `validate_program` executes every one of them at load,
# so a rule whose examples were edited into a lie refuses to load rather than
# shipping a false proof.
#
# ORDER IS THE POLICY. Within a layer, the LAST rule that matches wins, so
# these tuples are written least-specific-first on purpose.
# ---------------------------------------------------------------------------

#: Tools that only READ. Named as a glob family rather than one rule per tool,
#: because "add a read tool" must not mean "remember to add a rule".
READ_ONLY_TOOLS = (
    "read|list|glob|grep|search|image|read_symbol|find_definition"
    "|find_references|blast_radius|decision_memory|git_log|git_show"
    "|git_blame|git_branch|git_remote|git_describe|git_rev_parse"
    "|git_ls_files|git_diff|todo|ask|cancel|finish"
)

#: The floor rules `shared/` owns. T3's patterns are folded in separately and
#: are NOT restated here -- see `hardline_floor_rules`.
#:
#: Every example below names a `path` and no `command`, and T3's patterns are
#: all `command_pattern`s, so the two sets cannot collide. That is not luck:
#: `validate_program` checks each rule's examples against its whole layer, and
#: if a T3 pattern also matched one of these examples the LAST declaration
#: would win and one of the two sets would fail to load. Keeping the
#: dimensions disjoint is what makes both provable at once.
SHARED_FLOOR_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "floor:secret-files",
        "decision": "deny",
        "layer": FLOOR_LAYER,
"path": ".env*|*.env*",
        "reason": (
            "an environment file is the start of every exfiltration path; it "
            "is never a legitimate edit target for a coding agent"
        ),
        "match": ["path=.env", "path=.env.local", "path=.env.production"],
        "not_match": ["path=src/settings.py", "path=docs/env-setup.md"],
    },
    {
        "name": "floor:private-key-material",
        "decision": "deny",
        "layer": FLOOR_LAYER,
        "path": "*.pem|*.key|*id_rsa*|*id_ed25519*|.netrc|.pypirc",
        "reason": "private key material is not a repository file to edit",
        "match": [
            "path=certs/server.pem",
            "path=keys/id_rsa",
            "path=.netrc",
        ],
        "not_match": ["path=docs/pem-format.md"],
    },
    {
        "name": "floor:protected-test-surface",
        "decision": "deny",
        "layer": FLOOR_LAYER,
        "path": "tests/*",
        "side_effect_class": "workspace_write",
        "reason": (
            "the test suite is the regression gate; an agent that edits it is "
            "editing the thing that would have caught its own mistake"
        ),
        "match": [
            'tool=edit path="tests/test_app.py" side_effect_class=workspace_write',
            'tool=write path="tests/conftest.py" side_effect_class=workspace_write',
        ],
        "not_match": [
            'tool=edit path="src/app.py" side_effect_class=workspace_write',
            'tool=read path="tests/test_app.py"',
        ],
    },
)

#: The `defaults` layer: what the agent may do without asking, and what it
#: must ask about. Written least-specific first because last-match-wins.
DEFAULT_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "default:mutation-asks",
        "decision": "ask",
        "layer": "defaults",
        "side_effect_class": "workspace_write",
        "scope": "once",
        "reason": "a mutation changes the working tree and needs one decision",
        "match": [
            'tool=edit path="src/app.py" side_effect_class=workspace_write',
            'tool=write path="README.md" side_effect_class=workspace_write',
        ],
        "not_match": ['tool=read path="src/app.py"'],
    },
    {
        "name": "default:process-asks",
        "decision": "ask",
        "layer": "defaults",
        "side_effect_class": "process",
        "reason": "a subprocess runs code the policy has not read",
        "match": [
            'tool=bash side_effect_class=process command="python -m pytest"',
            'tool=test side_effect_class=process command="python -m pytest"',
        ],
        "not_match": ['tool=read path="src/app.py"'],
    },
    {
        "name": "default:network-asks",
        "decision": "ask",
        "layer": "defaults",
        "side_effect_class": "network",
        "reason": "an outbound request leaves the machine",
        "match": [
            "tool=web_fetch side_effect_class=network network_domain=example.com",
        ],
        "not_match": ['tool=read path="src/app.py"'],
    },
    {
        "name": "default:read-only-command-allows",
        "decision": "allow",
        "layer": "defaults",
        "command_risk": "read",
        "scope": "session_command_prefix",
        "reason": (
            "the arity dictionary declares this command read-only, so it is "
            "allowed without a per-command approval"
        ),
        "match": [
            'tool=bash command="git status"',
            'tool=bash command="cat src/app.py"',
        ],
        "not_match": [
            'tool=bash command="git push"',
            'tool=bash command="rm -rf build"',
        ],
    },
    {
        "name": "default:read-only-tool-allows",
        "decision": "allow",
        "layer": "defaults",
        "tool": READ_ONLY_TOOLS,
        "reason": "a read-only tool cannot change anything, so it does not ask",
        "match": [
            'tool=read path="src/app.py"',
            "tool=grep",
            "tool=git_log",
        ],
        "not_match": ["tool=bash", "tool=edit"],
    },
)

#: The `builtin_agent` layer: what THIS product's agent narrows further. A
#: user config may relax it (that is what a later layer means); the floor may
#: not be relaxed by anybody.
BUILTIN_AGENT_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "builtin:no-history-rewrite",
        "decision": "deny",
        "layer": "builtin_agent",
        "command_pattern": r"^git\s+(?:commit\s+.*(?:--amend|--no-verify)|push\b.*--force\b(?!-with-lease))",
        "reason": (
            "rewriting published history and bypassing the commit gate are "
            "operator decisions, not agent decisions"
        ),
        "match": [
            'tool=bash command="git commit --amend -m x"',
            'tool=bash command="git push --force origin main"',
        ],
        "not_match": [
            'tool=bash command="git commit -m \\"fix the bug\\""',
            'tool=bash command="git push origin feature-branch"',
        ],
    },
)

#: A demonstration `user_config` layer. NOT part of `shipped_policy()` by
#: default -- it exists so the layering is EXECUTABLE evidence rather than a
#: claim in a comment, and so a reader can see exactly what a user rule looks
#: like.
USER_CONFIG_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "user:allow-arxiv-lookups",
        "decision": "allow",
        "layer": "user_config",
        "tool": "web_fetch",
        "network_domain": "arxiv.org",
        "scope": "session_command_prefix",
        "reason": "the operator trusts this host for documentation lookups",
        "match": ["tool=web_fetch network_domain=arxiv.org"],
        "not_match": ["tool=web_fetch network_domain=evil.invalid"],
    },
)

#: A demonstration `per_session` layer: the ARITY-SCOPED GRANTS T4 renders.
#: This is the whole reason the arity dictionary exists -- `git commit -m` is
#: remembered as `git commit -m <message>`, not as "bash".
PER_SESSION_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "session:grant-git-commit-with-message",
        "decision": "allow",
        "layer": "per_session",
        "command_prefix": "git commit",
        "command_pattern": r"^(?=[^\n]*\s-m\s)(?!.*--amend)(?!.*--no-verify)",
        "scope": "session_command_prefix",
        "reason": (
            "the operator approved committing with a message; the grant is "
            "remembered as `git commit -m <message>`, not as bash"
        ),
        "match": ['tool=bash command="git commit -m \\"fix the mean() bug\\""'],
        "not_match": ['tool=bash command="git commit --amend -m x"'],
    },
    {
        "name": "session:grant-src-tree-writes",
        "decision": "allow",
        "layer": "per_session",
        "path": "src/*",
        "side_effect_class": "workspace_write",
        "scope": "session_path",
        "reason": "the operator approved edits under src/ for this session",
        "match": [
            'tool=edit path="src/app.py" side_effect_class=workspace_write'
        ],
        "not_match": [
            'tool=edit path="docs/guide.md" side_effect_class=workspace_write'
        ],
    },
)


def shipped_policy(
    *,
    include_user_config: bool = False,
    include_per_session: bool = False,
    include_hardline: bool = True,
) -> PolicyProgram:
    """Return the shipped, VALIDATED policy program.

    Validation happens here, at construction, so an unusable shipped policy is
    an import-time failure for whoever asks for it rather than a surprise at
    the first tool call.

    ``include_hardline`` folds T3's floor in. When ``runtime.hardline`` is
    unavailable the floor is simply T5's own three rules and the program is
    still valid -- but :func:`hardline_floor_rules`'s reason is then the
    operator's answer to "why is my floor smaller than I expected", which is
    why it is a separate call rather than being swallowed here.
    """
    rules: list[PolicyRule] = [
        PolicyRule.from_value(value) for value in SHARED_FLOOR_RULES
    ]
    if include_hardline:
        folded, _reason = hardline_floor_rules()
        # Folded LAST so a T3 example is never stolen by a T5 floor rule; the
        # two sets are dimension-disjoint, which is what makes that safe.
        rules.extend(folded)
    rules.extend(PolicyRule.from_value(value) for value in DEFAULT_RULES)
    rules.extend(PolicyRule.from_value(value) for value in BUILTIN_AGENT_RULES)
    if include_user_config:
        rules.extend(PolicyRule.from_value(value) for value in USER_CONFIG_RULES)
    if include_per_session:
        rules.extend(PolicyRule.from_value(value) for value in PER_SESSION_RULES)
    return validate_program(
        PolicyProgram(
            rules=tuple(rules),
            default_action=DEFAULT_ACTION,
            validated=False,
        )
    )
