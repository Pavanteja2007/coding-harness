"""Sub-step decomposition, and the test that a step is INDEPENDENTLY VERIFIABLE.

`project-spec.md` item 14 is *"task decomposition into small, independently
verifiable sub-steps -- avoids 'one-shotting'."* Two halves of that sentence,
and the second is the one that is usually missing.

A list of steps is not a decomposition. It is a to-do list, and it can be
trivially satisfied by a model that emits five headings and one enormous edit.
What makes a decomposition real is that **each step can be checked on its own**,
without the other steps having run.

**What is checked here, and what is not.** This module is a SCHEMA validator
plus a decision function over data. It cannot prove a step's verification will
pass -- only a run can. What it can decide, mechanically, is whether the step
*declares* a check that could be executed without its siblings' output, which
is the property item 14 names. A plan that fails this is a plan, and the
receipt says so.

**Three refusals, and the third is the interesting one.**

``no_verification``
    The step names no way to check itself. Indistinguishable from "do the
    work", which is the one-shotting failure item 14 exists to prevent.

``verification_not_scoped``
    The step's check cannot be attributed to the step. ``run the tests`` after
    changing the parser is a check of the whole program; if it fails you do not
    know whether this step is finished. **Whole-suite checks are not
    independent verification** -- they are the reason plans get reworked, and
    this is the finding that took a rewrite of the predicate to accept.

``ordering_dependent``
    The step's check needs a sibling's output and does not say so. This is
    LEGITIMATE work -- a migration genuinely cannot be checked before the
    schema lands -- so it is allowed *when declared*. What is refused is an
    UNDECLARED dependency, because that is how a plan ends with two steps that
    each look finished and neither is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

#: The closed set of ways a plan may fail the independence test. A reader can
#: branch on the reason without parsing prose.
DECOMPOSITION_REFUSALS: Tuple[str, ...] = (
    "no_verification",
    "verification_not_scoped",
    "ordering_dependent",
    "no_steps",
    "step_is_too_broad",
)

#: Verbs a verification command may use and still count as SCOPED. A check
#: naming one of these is pointing at a target: one file, one test node, one
#: symbol. A check that names none of them is measuring the whole program and
#: this module refuses to call it independent verification.
#:
#: This is a judgement, published so a reviewer can move a name. It is NOT a
#: claim about what a command does -- it is a claim about whether the command
#: NAMES what it is checking, which is decidable from the string.
SCOPED_VERIFICATION_VERBS: Tuple[str, ...] = (
    "pytest",
    "py.test",
    "unittest",
    "npm",
    "npx",
    "node",
    "go",
    "cargo",
    "mvn",
    "gradle",
    "rspec",
    "phpunit",
    "make",
    "ruff",
    "mypy",
    "tsc",
    "eslint",
    "jest",
    "vitest",
)

#: Commands that are scoped by their mere presence: they take a target as an
#: argument by construction, so a bare invocation is still a scoped check.
IMPLICITLY_SCOPED_COMMANDS: Tuple[str, ...] = (
    "grep",
    "rg",
    "python -c",
    "python3 -c",
)

#: A step whose description is longer than this many characters, or which names
#: more than this many files, is refused as too broad. Published as a constant
#: rather than a literal because the bound is a judgement somebody has to be
#: able to find and move.
MAX_STEP_DESCRIPTION_CHARS = 400
MAX_STEP_FILES = 12

#: Bare subcommand words that follow a scoped verb and are NOT a target.
#: ``npm test`` runs the whole suite; ``npm test -- src/a.ts`` names one file.
#: Without this table the first would read as scoped, which is precisely the
#: whole-suite check this module exists to reject -- and it is the single most
#: common shape in real plans.
BARE_SUBCOMMANDS: Tuple[str, ...] = (
    "test",
    "tests",
    "check",
    "lint",
    "build",
    "run",
    "install",
    "start",
    "clean",
    "all",
    "verbose",
    "quiet",
    "watch",
    "default",
    "ci",
    "unit",
    "integration",
    "e2e",
)

#: Characters that make an argument PATH-SHAPED, and therefore a target rather
#: than a flag or a subcommand. A target in practice is a file, a node id, a
#: package path or a symbol reference, and every one of those carries one of
#: these.
_PATHISH = ("/", "\\", ".", ":", "@", "#", "*")

__all__ = [
    "BARE_SUBCOMMANDS",
    "DECOMPOSITION_REFUSALS",
    "IMPLICITLY_SCOPED_COMMANDS",
    "MAX_STEP_DESCRIPTION_CHARS",
    "MAX_STEP_FILES",
    "SCOPED_VERIFICATION_VERBS",
    "DecompositionRefusal",
    "DecompositionVerdict",
    "SubStep",
    "SubStepVerdict",
    "decomposition_verdict",
    "is_scoped_verification",
    "new_substep",
    "substep_from_dict",
]


@dataclass(frozen=True)
class SubStep:
    """One sub-step of a plan, with the check that closes it."""

    id: str
    description: str = ""
    files: Tuple[str, ...] = ()
    verify: str = ""
    depends_on: Tuple[str, ...] = ()
    status: str = "pending"

    def __post_init__(self) -> None:
        identifier = str(self.id or "").strip()
        if not identifier:
            raise ValueError("a sub-step needs an id")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "files", tuple(str(item) for item in (self.files or ())))
        object.__setattr__(
            self, "depends_on", tuple(str(item) for item in (self.depends_on or ()))
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this step."""
        return {
            "id": self.id,
            "description": self.description,
            "files": list(self.files),
            "verify": self.verify,
            "depends_on": list(self.depends_on),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubStep":
        """Rebuild a step from a JSON projection, tolerating absent keys."""
        data = dict(value or {})
        return cls(
            id=str(data.get("id") or data.get("step") or ""),
            description=str(data.get("description") or ""),
            files=tuple(data.get("files") or data.get("files_hint") or ()),
            verify=str(data.get("verify") or data.get("verification") or ""),
            depends_on=tuple(data.get("depends_on") or ()),
            status=str(data.get("status") or "pending"),
        )


def new_substep(
    step_id: str,
    description: str,
    *,
    files: Sequence[str] = (),
    verify: str = "",
    depends_on: Sequence[str] = (),
) -> SubStep:
    """Build one sub-step.

    ``verify`` is a required-in-practice argument rather than a required
    constructor argument, because a plan produced by a model arrives without
    one and the refusal belongs in
    :func:`decomposition_verdict` where it can name the step -- not in a
    ``ValueError`` that would kill the run over one step of five.
    """
    return SubStep(
        id=step_id,
        description=description,
        files=tuple(files),
        verify=str(verify or ""),
        depends_on=tuple(depends_on),
    )


#: Kept as the public spelling used by the plan parser.
substep_from_dict = SubStep.from_dict


def is_scoped_verification(command: Any) -> bool:
    """Return whether a check NAMES what it is checking.

    Decidable from the string alone, which is why it is a function and not a
    claim. The test is "does the command address a target?" -- a node id, a
    file, a symbol, a module -- and the evidence is a scoped verb or a
    grep-shaped command plus at least one argument that is not a bare flag.

    ``python -m pytest`` with no arguments is REFUSED. It is the single most
    common shape in real plans and it is exactly the whole-suite check this
    module exists to reject: it passes when every other step is done, so it
    cannot tell this step from the other four.
    """
    text = str(command or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(lowered.startswith(name) for name in IMPLICITLY_SCOPED_COMMANDS):
        # A grep with a pattern IS scoped even with no file argument: the
        # pattern is the target.
        return True
    for verb in SCOPED_VERIFICATION_VERBS:
        if verb not in lowered:
            continue
        rest = lowered.split(verb, 1)[1]
        if verb == "make" and rest.strip() and not rest.strip().startswith("-"):
            # A make target names what it builds, so it is scoped by itself.
            return True
        for token in rest.replace("=", " ").split():
            if not token or token.startswith("-"):
                continue
            if token in BARE_SUBCOMMANDS:
                continue
            if any(mark in token for mark in _PATHISH):
                return True
        return False
    return False


@dataclass(frozen=True)
class DecompositionRefusal:
    """One refused plan or step, with the reason and the owner."""

    reason: str
    detail: str
    subject: str = ""
    owner: str = "T1 / harness"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this refusal."""
        return {"reason": self.reason, "detail": self.detail, "subject": self.subject,
                "owner": self.owner}

    def render(self) -> str:
        """Return the one quotable line."""
        subject = f" [{self.subject}]" if self.subject else ""
        return f"NOT A DECOMPOSITION ({self.reason}){subject}: {self.detail}"


@dataclass(frozen=True)
class SubStepVerdict:
    """One step's answer: is it independently verifiable?"""

    step_id: str
    independently_verifiable: bool
    reason: str = ""
    depends_on: Tuple[str, ...] = ()
    declared_dependency: bool = False
    too_broad: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this verdict."""
        return {
            "step_id": self.step_id,
            "independently_verifiable": bool(self.independently_verifiable),
            "reason": self.reason,
            "depends_on": list(self.depends_on),
            "declared_dependency": bool(self.declared_dependency),
            "too_broad": bool(self.too_broad),
        }


@dataclass(frozen=True)
class DecompositionVerdict:
    """The plan's answer, and the one number a caller branches on.

    ``is_decomposition`` is False when ANY step fails. A partially-independent
    plan is a plan: five steps where four are checkable and one is not has a
    step nobody can close, and reporting ``4/5`` as a pass is how a
    decomposition quietly stops being one.
    """

    is_decomposition: bool
    steps: Tuple[SubStepVerdict, ...] = ()
    refusals: Tuple[DecompositionRefusal, ...] = ()
    verifiable: int = 0
    total: int = 0

    @property
    def verdict(self) -> str:
        """Return one of a closed set of words, never a bare boolean."""
        if not self.steps:
            return "no_steps"
        return "decomposition" if self.is_decomposition else "plan_not_progress"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this verdict."""
        return {
            "is_decomposition": bool(self.is_decomposition),
            "verdict": self.verdict,
            "verifiable": int(self.verifiable),
            "total": int(self.total),
            "steps": [item.to_dict() for item in self.steps],
            "refusals": [item.to_dict() for item in self.refusals],
        }

    def render(self) -> str:
        """Return plain lines a reader can act on."""
        lines = [
            f"decomposition: {self.verdict} ({self.verifiable}/{self.total} "
            "independently verifiable)"
        ]
        for item in self.steps:
            mark = "ok " if item.independently_verifiable else "NO "
            lines.append(f"  {mark} {item.step_id}: {item.reason}")
        for refusal in self.refusals:
            lines.append(f"  {refusal.render()}")
        return "\n".join(lines)


def decomposition_verdict(
    steps: Sequence[SubStep | Mapping[str, Any]],
) -> DecompositionVerdict:
    """Return whether this is a decomposition or merely a plan.

    The whole test, and it is deliberately mechanical so that a plan cannot
    pass by sounding convincing:

    1. at least one step;
    2. every step declares a ``verify``;
    3. every ``verify`` NAMES a target (:func:`is_scoped_verification`);
    4. every dependency between steps is DECLARED in ``depends_on`` -- an
       undeclared one is inferred from file overlap and refused;
    5. no step is so broad that one turn could not finish it.

    Rule 4 is the one worth reading twice. Shared files between two steps do
    NOT prove a dependency -- two steps can touch one file sequentially -- so
    file overlap alone is reported as a warning on the step rather than a
    refusal. What IS refused is a dependency a step admits to in prose
    ("after the schema lands") while declaring nothing, because that is the
    case where a step looks finished and is not.
    """
    normalised: List[SubStep] = []
    for raw in steps or ():
        if isinstance(raw, SubStep):
            normalised.append(raw)
        elif isinstance(raw, Mapping):
            try:
                normalised.append(SubStep.from_dict(raw))
            except (ValueError, TypeError):
                continue
    if not normalised:
        return DecompositionVerdict(
            is_decomposition=False,
            refusals=(
                DecompositionRefusal(
                    reason="no_steps",
                    detail="a plan with no sub-steps is a wish, not a decomposition",
                ),
            ),
        )

    known_ids = {item.id for item in normalised}
    verdicts: List[SubStepVerdict] = []
    refusals: List[DecompositionRefusal] = []

    for step in normalised:
        reasons: List[str] = []
        ok = True

        if not str(step.verify or "").strip():
            ok = False
            reasons.append("declares no verification")
            refusals.append(
                DecompositionRefusal(
                    reason="no_verification",
                    detail=(
                        f"sub-step {step.id!r} names no way to check itself; a step "
                        "nobody can check is not a sub-step"
                    ),
                    subject=step.id,
                )
            )
        elif not is_scoped_verification(step.verify):
            ok = False
            reasons.append(
                f"verification {step.verify!r} names no target, so it cannot tell "
                "this step from the others"
            )
            refusals.append(
                DecompositionRefusal(
                    reason="verification_not_scoped",
                    detail=(
                        f"sub-step {step.id!r} verifies with {step.verify!r}, which "
                        "measures the whole program. Name the target: one file, one "
                        "test node, one symbol"
                    ),
                    subject=step.id,
                )
            )

        undeclared = sorted({dep for dep in step.depends_on if dep not in known_ids})
        if undeclared:
            ok = False
            reasons.append("depends on steps that do not exist: " + ", ".join(undeclared))
            refusals.append(
                DecompositionRefusal(
                    reason="ordering_dependent",
                    detail=(
                        f"sub-step {step.id!r} depends on unknown step(s) "
                        f"{', '.join(undeclared)}"
                    ),
                    subject=step.id,
                )
            )

        prose_dependency = _prose_names_a_sibling(step, known_ids)
        if prose_dependency and not step.depends_on:
            ok = False
            reasons.append(
                "describes an ordering on " + ", ".join(prose_dependency)
                + " but declares no depends_on"
            )
            refusals.append(
                DecompositionRefusal(
                    reason="ordering_dependent",
                    detail=(
                        f"sub-step {step.id!r} names sibling step(s) "
                        f"{', '.join(prose_dependency)} in its description while "
                        "declaring no dependency. Declare it: a declared dependency is "
                        "legitimate work, an undeclared one is a step that looks done"
                    ),
                    subject=step.id,
                )
            )

        too_broad = len(str(step.description or "")) > MAX_STEP_DESCRIPTION_CHARS or len(
            step.files
        ) > MAX_STEP_FILES
        if too_broad:
            ok = False
            reasons.append(
                f"too broad to finish in one turn ({len(step.description)} chars, "
                f"{len(step.files)} files)"
            )
            refusals.append(
                DecompositionRefusal(
                    reason="step_is_too_broad",
                    detail=(
                        f"sub-step {step.id!r} exceeds {MAX_STEP_DESCRIPTION_CHARS} "
                        f"chars or {MAX_STEP_FILES} files; split it"
                    ),
                    subject=step.id,
                )
            )

        verdicts.append(
            SubStepVerdict(
                step_id=step.id,
                independently_verifiable=ok,
                reason="; ".join(reasons) if reasons else "declares a scoped verification",
                depends_on=tuple(step.depends_on),
                declared_dependency=bool(step.depends_on),
                too_broad=too_broad,
            )
        )

    verifiable = sum(1 for item in verdicts if item.independently_verifiable)
    return DecompositionVerdict(
        is_decomposition=verifiable == len(verdicts),
        steps=tuple(verdicts),
        refusals=tuple(refusals),
        verifiable=verifiable,
        total=len(verdicts),
    )


#: Words that announce an ordering without a machine-readable dependency.
_ORDERING_CUES: Tuple[str, ...] = (
    "after ",
    "once ",
    "following ",
    "depends on",
    "requires step",
    "then ",
)


def _prose_names_a_sibling(step: SubStep, known_ids: set) -> Tuple[str, ...]:
    """Return the sibling step ids this step's PROSE names.

    Deliberately narrow: a step id appearing in its own description is
    ignored, and the search is for an explicit ordering cue plus a sibling id
    rather than for any mention. A looser rule would refuse every plan whose
    descriptions cross-reference, which is a guard that gets turned off.
    """
    lowered = str(step.description or "").lower()
    if not any(cue in lowered for cue in _ORDERING_CUES):
        return ()
    return tuple(
        sorted(
            identifier
            for identifier in known_ids
            if identifier != step.id and identifier.lower() in lowered
        )
    )
