"""Behavioural policy, in CONFIGURATION — and the reason it left the prompt.

The argument
------------
The corpus records where the industry is going, and it is not towards longer
prompts: *"Codex's newest model prompts dropped the no-commit and
anti-gold-plating directives in favor of feature flags. Mistral Vibe deleted its
'Never Commit' hard rule and re-founded its prompt on a seven-level precedence
contract"* — **"behavioural policy is moving from the prompt (where the model
reads it) to configuration (where the platform enforces it)."**

The sentence's own contrast is the whole of it. A prompt is a **request**; a
policy engine is a **guarantee**. And this repository already measured what
prompt prose costs on its own: **AHEAD ablated system-prompt prose at −2.3pp.**
Every behavioural sentence moved from prose into prose was paid for twice —
once in the token budget, once in the measured quality delta.

So this module is where a behavioural directive is **declared**, and its
defining property is that it is inert:

:class:`BehaviourPolicy` holds no text for the model to read. It holds, for each
declared directive, **the mechanism that enforces it** — and :meth:`refuse` is
the one place a directive with no mechanism is rejected.

The three states, kept apart
-----------------------------
``declared`` and ``enforced`` are different facts and conflating them is how a
"policy" becomes a decoration:

* **not declared** — the operator has said nothing. Nothing changes, and this
  is the default.
* **declared and enforced** — the operator named it AND a real mechanism
  enforces it. The mechanism is named in the receipt.
* **declared and NOT enforced** — :meth:`BehaviourPolicy.refuse` rejects this.
  A directive with no enforcing mechanism IS a prompt line wearing a config
  key's clothes, and accepting it would create a second, dishonest place to
  state policy: a user would read ``"no_commit": true`` and believe the agent
  cannot commit, when what it actually means is that a string somewhere asks
  nicely.

The no-commit case specifically
-------------------------------
``no_commit`` is the canonical example and the reason the corpus names it. In
this repository it is **already** structurally enforced:
``harness.tools.ToolSpec.requires_approval`` is ``True`` on ``git_commit``'s
family, and the approval model is a Phase-2 guarantee rather than an
instruction. So the configuration key here does not turn enforcement on — it
**reports** it, and it refuses to claim enforcement for a directive the
mechanism does not actually cover.

A prompt variant can never widen a capability
---------------------------------------------
This is Phase 2's invariant and the phase brief restates it: *"A prompt that
says 'don't commit' is a request; a policy that blocks commit is a guarantee."*
The separation is asserted structurally by
:func:`widenings_possible`, which is what a test calls: it compares the
capability set a run would have under each prompt variant and returns every
tool the variant ADDS. A prompt has no vocabulary for adding a tool — but a
prompt variant registry is exactly the kind of thing that grows a `tools: [...]`
field one day, and the check is cheap insurance that says so out loud.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BEHAVIOUR_KEYS",
    "ENFORCEMENT",
    "BehaviourPolicy",
    "Directive",
    "policy_from_config",
    "widenings_possible",
]


#: The closed set of directives this layer can declare. Closed because a policy
#: language that accepts any key is a place where "no-commit" and
#: "definitely-do-commit" can both be spelled, and a reader has no way to tell
#: which one a given key meant.
BEHAVIOUR_KEYS: Tuple[str, ...] = (
    "no_commit",
    "read_before_edit",
    "no_gold_plating",
    "no_test_edits",
)

#: What enforces each directive TODAY, in this repository.
#:
#: Every value names a real mechanism with a real refusal path. An empty value
#: would mean "no enforcement exists", and a directive whose enforcement is empty
#: is refused rather than declared — see :meth:`BehaviourPolicy.refuse`.
#:
#: The mapping is the POINT of this module. It is a claim that can be checked,
#: printed, and diffed, and it says exactly which Phase-2 mechanism a user is
#: relying on when they declare a directive. Without it, "the policy layer
#: enforces no-commit" is a claim about a prompt.
ENFORCEMENT: Dict[str, str] = {
    "no_commit": (
        "harness.tools.ToolSpec.requires_approval on every VCS write, plus the "
        "Phase-2 approval model (harness.agent_kernel.policy.PolicyEngine). A "
        "commit with no approval grant is a DENY, not a request."
    ),
    "read_before_edit": (
        "harness.editor's expected_revision precondition plus the stale_read "
        "refusal in harness.agent_kernel.tools: a mutation of a file this run "
        "never read is REFUSED before dispatch."
    ),
    "no_gold_plating": (
        "harness.tool_surface.surface_violations, which REFUSES a surface that "
        "withholds a tool without offering a replacement - so an agent cannot "
        "be handed a surface that makes the minimal fix impossible."
    ),
    "no_test_edits": (
        "harness.editor.is_protected plus Ceiling-R2-03's test-configuration "
        "guard: tests/* and test_*.py are protected paths, and editing pytest "
        "configuration without a declared reason is refused."
    ),
}


@dataclass(frozen=True)
class Directive:
    """One declared directive and the mechanism that actually enforces it."""

    key: str
    declared: bool = False
    enforced: bool = False
    enforcement: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection.

        ``declared`` and ``enforced`` are separate keys and a reader needs both.
        A projection that published only the first would render "declared" and
        "guaranteed" identically, which is the exact confusion this module
        exists to remove.
        """
        return {
            "key": self.key,
            "declared": bool(self.declared),
            "enforced": bool(self.enforced),
            "enforcement": self.enforcement,
            "note": self.note,
        }


@dataclass(frozen=True)
class BehaviourPolicy:
    """The declared behavioural policy for one run. Carries no model-facing text."""

    directives: Tuple[Directive, ...] = ()
    source: str = ""

    @property
    def declared_keys(self) -> Tuple[str, ...]:
        """Return the declared directive keys, sorted."""
        return tuple(
            sorted(d.key for d in self.directives if d.declared)
        )

    @property
    def any_declared(self) -> bool:
        """Whether the operator declared anything at all.

        ``False`` is the shipped default and it means the run's behaviour is
        decided entirely by the enforcement mechanisms, exactly as it was before
        this module existed.
        """
        return any(d.declared for d in self.directives)

    def refuse(self, key: str) -> str:
        """Return why ``key`` may not be declared, or ``""`` if it may.

        Two refusals, and the second is the interesting one:

        * an unknown key — the vocabulary is closed, so a typo cannot become a
          policy nobody reviews;
        * a known key whose enforcement this repository does not actually
          provide — the honest case. It is not a "not yet implemented"; it is
          "declaring this would put a promise in your config that the harness
          cannot keep", which is worse than not having the key at all.
        """
        clean = str(key or "").strip().lower()
        if clean not in BEHAVIOUR_KEYS:
            return (
                f"{clean!r} is not a known behavioural directive; the closed "
                f"vocabulary is {list(BEHAVIOUR_KEYS)}"
            )
        if not ENFORCEMENT.get(clean):
            return (
                f"{clean!r} has no enforcing mechanism in this harness, so "
                "declaring it would state a promise the platform cannot keep. "
                "It belongs in the prompt ONLY as an explicitly-labelled "
                "request, never as configuration."
            )
        return ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "source": self.source,
            "any_declared": self.any_declared,
            "declared": list(self.declared_keys),
            "directives": [d.to_dict() for d in self.directives],
        }

    def render(self) -> str:
        """Return the model-facing text for this policy.

        **Empty unless a directive is BOTH declared and unenforced**, and
        :meth:`refuse` means no such directive can reach here through
        :func:`policy_from_config`. The method exists to make the emptiness
        structural rather than aspirational: a caller that renders behavioural
        policy into a prompt gets ``""`` from the only function that could
        produce anything, and a test asserts it.

        This is the sentence the phase brief asks for, made executable: the
        behavioural text moved OUT of the prompt and INTO this configuration,
        and what remains in the prompt is nothing.
        """
        return ""


def policy_from_config(
    config: Optional[Mapping[str, Any]],
) -> BehaviourPolicy:
    """Return the run's declared behavioural policy.

    Reads four flat keys from the run config. A key whose value is false-y is
    treated as NOT declared, using
    :func:`harness.tool_surface.truthiness_of` — this repository has already
    shipped two spellings of "is this config value on" and a third would be a
    third answer.

    A declared key with no enforcement is **dropped and named**, not silently
    ignored: the receipt's ``note`` says which declaration was refused and why,
    because a user who wrote ``no_gold_plating: true`` and got silence has no
    way to tell a working directive from a typo.
    """
    from harness.tool_surface import truthiness_of

    directives: List[Directive] = []
    notes: List[str] = []
    for key in BEHAVIOUR_KEYS:
        declared = bool(config and key in config and truthiness_of(config.get(key)))
        mechanism = ENFORCEMENT.get(key, "")
        directives.append(
            Directive(
                key=key,
                declared=declared,
                enforced=bool(declared and mechanism),
                enforcement=mechanism,
                note="" if (not declared or mechanism) else "refused: no mechanism",
            )
        )
    policy = BehaviourPolicy(directives=tuple(directives), source="config")
    for directive in directives:
        if directive.note:
            notes.append(f"{directive.key}: {directive.note}")
    if notes:
        policy = BehaviourPolicy(
            directives=directives,
            source="config; " + "; ".join(notes),
        )
    return policy


def widenings_possible(
    base_tools: Sequence[str],
    variant_text: str = "",
    variant_tools: Optional[Iterable[str]] = None,
) -> List[str]:
    """Return tools a prompt variant would ADD, which must always be empty.

    The check the phase brief asks for, as a function a test can call. Two
    directions are checked and only one can be non-empty by construction:

    * ``variant_tools`` — an explicit tool list on a prompt variant. Any name
      in it that is not in ``base_tools`` is a widening, because the prompt
      layer has no authority to grant a tool.
    * ``variant_text`` — the variant's TEXT. A prompt cannot grant a capability
      in prose, so the only thing worth checking is that the text does not
      *assert* one. This looks for the shape of such a claim rather than
      pretending to prove its absence: a directive that tells the model it MAY
      do something the base surface does not offer. It is a lint on prompt
      authors, not a proof, and its docstring says so.

    Returns the offending names, sorted and deduplicated. An empty list is the
    only passing answer.
    """
    base = {str(name) for name in base_tools or ()}
    added = sorted({str(name) for name in (variant_tools or ())} - base)
    text = str(variant_text or "").lower()
    if text:
        # The shape of a prompt that grants rather than describes. Deliberately
        # narrow: these are the phrasings an author reaches for when they are
        # trying to add capability in prose.
        for phrase in (
            "you may also use",
            "you are allowed to use",
            "add this tool",
            "additional tool",
            "you now have access to",
        ):
            if phrase in text:
                added.append(f"<prompt text asserts capability: {phrase!r}>")
    return sorted(set(added))
