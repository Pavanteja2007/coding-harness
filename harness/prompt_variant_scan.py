"""T1.W2.4 — no prompt variant can change what the agent may do.

THE INVARIANT, AND WHY IT IS FRAGILE RIGHT NOW
----------------------------------------------
Phase 2's invariant is that **behaviour is decided by policy, not by prose.**
Wave 1 kept it by moving the behavioural directives out of the prompt and into
:mod:`harness.behaviour_policy`, whose ``render()`` is structurally empty.

What changed since Phase 2 is that there are now **nine prompt variants** in
play — the seven agent-facing constants in :mod:`harness.prompts` plus the two
layers Wave 1 added (the per-family format matrix in
:mod:`harness.prompt_matrix`, and the capability-surface note). The corpus's
warning about the direction this is heading is sharp and it is about exactly
this:

    *"Codex's newest model prompts dropped the no-commit and anti-gold-plating
    directives in favour of feature flags … policy is moving from the prompt to
    configuration, and that direction must not reverse."*

So this module is not a formality. Nine variants means nine chances to put a
directive back, and the interesting question is not "is there a bad sentence"
— it is **whether a bad sentence could do any harm even if it were written.**
A prompt cannot add a tool. It cannot relax a gate. It cannot change a
permission. The only thing a prompt *can* do is lie, and a lie that grants
capability is the failure this phase exists to prevent.

WHAT IS SCANNED, AND HOW
------------------------
Every prompt-bearing constant is located by **AST**, not by grep and not by a
hand-kept name list. That is not tidiness: a hand-kept list of prompt names is a
list that stops being true the moment somebody adds a prompt, and the failure it
produces is a scan that reports CLEAN on a tree that has grown a new prompt
variant. The enumeration is derived, so a new prompt is scanned by construction.

The classifier is a **closed three-way vocabulary**, and the three are not
collapsible:

======================  =====================================================
class                   meaning
======================  =====================================================
``format_fact``         how a model parses or emits. Legitimate in a prompt.
``behavioural``         how we want it to behave. **Never legitimate as
                        prose** — it is a candidate for conversion to
                        configuration, and it is listed, not deleted.
``capability_claim``    a statement that grants, permits or widens. **A
                        finding**, and the only class that is a defect.
======================  =====================================================

The middle class is the one the brief singles out: *"Any behavioural aspiration
in a prompt variant must be flagged for conversion to configuration — not
deleted, not shipped as prose."* Deleting them would destroy the record of what
the prompt was trying to say; shipping them is the regression. So they are
enumerated with their constant and their sentence, and the fix is named.

WHY THE THREE STRUCTURAL ASSERTIONS ARE STRUCTURAL
--------------------------------------------------
"Assert no prompt variant can widen a capability" is trivially true if the scan
only reads text, because a text scan cannot prove what a downstream consumer
will do with the string. The three assertions here do not read text:

1. **A prompt module cannot reach a capability source.** By AST: no
   prompt-bearing module may import ``capability_surface``,
   ``STRATEGY_WITHHELD``, the policy engine, or the registry, and no prompt
   constant may carry a tool list. A prompt that cannot name the authority
   cannot widen it.
2. **Permission modes are a function of the mode, not of the variant.**
   :func:`harness.agent_kernel.kernel.capability_receipt` is called once per
   mode under every prompt variant's config, and the receipts must be equal. A
   prompt variant that changed a permission would move a receipt.
3. **The verdict gate is unreachable from a prompt.** The mint sites are
   enumerated by AST across the whole tree and the set of files carrying one is
   asserted to exclude every prompt module, with the count pinned.

A reader who wants to break any of these has to edit production code, not a
string — which is the point. The prompt layer is where a widening is most
tempting and least detectable, so it is the layer given the fewest powers.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CAPABILITY_CLAIM_PHRASES",
    "CLASSES",
    "PROMPT_MODULES",
    "Finding",
    "VariantRow",
    "capability_widening_possible",
    "enumeration_table",
    "findings",
    "modes_are_identical_across_variants",
    "render_report",
    "scan",
    "scan_module",
    "variant_constants",
    "verdict_gate_untouched",
]


#: The modules whose string constants are model-facing text. **Derived, not
#: asserted**: :func:`variant_constants` walks this set and every additional
#: prompt-bearing module is a decision a reader can see.
PROMPT_MODULES: Tuple[str, ...] = (
    "harness/prompts.py",
    "harness/prompt_matrix.py",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The closed classification vocabulary. Read it before any count.
CLASSES: Tuple[str, ...] = ("format_fact", "behavioural", "capability_claim")

#: The phrasings that GRANT rather than describe. This is a lint on prompt
#: AUTHORS, not a proof that no capability claim exists — a proof is
#: :func:`capability_widening_possible`, which is structural. What this list
#: buys is that the common shape is caught at authoring time with a message that
#: says why.
#:
#: Each entry is a (phrase, why) pair so a finding can name the reason rather
#: than just quoting the string back.
CAPABILITY_CLAIM_PHRASES: Tuple[Tuple[str, str], ...] = (
    (
        "you may also use",
        "a prompt has no authority to grant a tool; the capability surface "
        "derives from capability_surface() and policy",
    ),
    (
        "you are allowed to use",
        "permission is a policy decision (harness.agent_kernel.policy), and a "
        "prompt cannot move it",
    ),
    ("add this tool", "a catalogue entry is added in harness/tools.py, never in a prompt"),
    ("additional tool", "the schema list is assembled by the strategy, never by a prompt"),
    ("you now have access to", "an access claim in prose is not an access grant"),
    (
        "you do not need approval",
        "this is the sharpest form: it tells the model a gate does not apply, "
        "and a model that believes it will attempt the action and be refused "
        "by a gate it was told was absent",
    ),
    (
        "skip the approval",
        "approval is enforced by PolicyEngine; a prompt cannot wave it through",
    ),
    (
        "without asking",
        "the same shape as the two above: a permission claim",
    ),
    (
        "you may ignore",
        "an instruction to bypass a refusal mechanism",
    ),
)

#: Phrasings that mark a BEHAVIOURAL aspiration — a preference about how the
#: agent should work. Not a defect on its own; the defect is shipping one as
#: prose. Each names where it belongs instead.
BEHAVIOURAL_PHRASES: Tuple[Tuple[str, str, str], ...] = (
    (
        "be concise",
        "verbosity control",
        "a model parameter or a response-length budget, not a prompt sentence; "
        "AHEAD measured prompt prose alone at -2.3pp",
    ),
    (
        "keep it minimal",
        "anti-gold-plating",
        "harness.behaviour_policy's no_gold_plating directive, enforced by "
        "tool_surface.surface_violations",
    ),
    (
        "do not commit",
        "no-commit",
        "harness.behaviour_policy's no_commit directive, enforced by "
        "ToolSpec.requires_approval plus PolicyEngine",
    ),
    (
        "do not modify tests",
        "protected tests",
        "harness.behaviour_policy's no_test_edits directive, enforced by "
        "editor.is_protected plus the R2-03 test-configuration guard",
    ),
    (
        "read the file first",
        "read-before-edit",
        "harness.behaviour_policy's read_before_edit directive, enforced by "
        "editor's expected_revision precondition and the stale_read refusal",
    ),
    (
        "only make the changes",
        "minimal-diff",
        "no enforcement mechanism exists for this one; it is aspirational and "
        "is listed as such rather than upgraded to a promise",
    ),
)


@dataclass(frozen=True)
class Finding:
    """One classified sentence from one prompt variant."""

    module: str
    constant: str
    line: int
    phrase: str
    classification: str
    why: str
    convert_to: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "module": self.module,
            "constant": self.constant,
            "line": int(self.line),
            "phrase": self.phrase,
            "classification": self.classification,
            "why": self.why,
            "convert_to": self.convert_to,
        }


@dataclass(frozen=True)
class VariantRow:
    """One prompt variant: where it lives and how big it is."""

    module: str
    constant: str
    line: int
    chars: int
    classification: str = "unclassified"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "module": self.module,
            "constant": self.constant,
            "line": int(self.line),
            "chars": int(self.chars),
            "classification": self.classification,
        }


# ---------------------------------------------------------------------------
# 1. Enumeration — derived by AST, never hand-kept
# ---------------------------------------------------------------------------


def _module_path(rel: str) -> Path:
    """Return the absolute path for a repo-relative module path."""
    return _REPO_ROOT / rel


def _string_constants(rel: str) -> List[Tuple[str, int, str]]:
    """Return ``(name, line, value)`` for every string constant in a module.

    **The enumeration is derived from the parse tree**, and that is the whole
    point. T5's own record of this round's sibling gate is the argument: every
    coverage entry initially included ``baseline``, ``baseline`` exists, so every
    edit to every prompt read as "armed" and a real unarmed edit passed. A
    hand-kept prompt list fails the same way — it stays true until it doesn't,
    and nothing reports the moment it stops being true.

    Docstrings are excluded, because a module's prose ABOUT capability claims is
    not a capability claim. Including them would make this module's own
    docstring — which quotes the phrasings it is looking for — a finding, and a
    scanner that reports itself is a scanner nobody reads.
    """
    path = _module_path(rel)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"{rel}: unreadable or unparseable ({exc})") from exc

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    out: List[Tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append((_enclosing_name(tree, node), int(node.lineno), node.value))
    return out


def _enclosing_name(tree: ast.AST, target: ast.AST) -> str:
    """Return the name a string constant belongs to.

    Three shapes are recognised, and all three are present in this tree's two
    prompt modules — which the first version of this function did not handle,
    so every string in ``prompt_matrix.py`` was attributed to ``<module>``:

    * a module-level (or class-level) ``NAME = "..."`` assignment;
    * a **constructor keyword argument**, which is how every
      :class:`harness.prompt_matrix.FormatFact` carries its text;
    * a plain literal argument, attributed to the call's callee name.

    Attribution matters because a finding that says ``<module>`` cannot be acted
    on, and a scan whose output cannot be acted on is a comment.
    """
    parents: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    cursor: Optional[ast.AST] = target
    call_node: Optional[ast.AST] = None
    while cursor is not None:
        parent = parents.get(id(cursor))
        if isinstance(parent, ast.keyword) and parent.arg:
            return parent.arg
        if isinstance(parent, ast.Call):
            if call_node is None:
                call_node = parent
            func = parent.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name:
                return f"{name}(...)"
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            # AnnAssign is a SEPARATE node with no ``targets`` list — it is
            # ``NAME: Type = value``. Omitting it left every string in
            # ``UNREACHABLE_OBSERVATIONS: Tuple[...] = (...)`` attributed to
            # ``<module>``, which is 17 of this tree's 69 variants and makes the
            # table unreadable for exactly the entries a reviewer most wants to
            # check: the observations that are deliberately NOT shipped.
            if isinstance(parent, ast.AnnAssign):
                annotated = getattr(parent.target, "id", None) or getattr(
                    parent.target, "attr", None
                )
                if annotated:
                    return annotated
            for slot in getattr(parent, "targets", []):
                target_name = getattr(slot, "id", None) or getattr(slot, "attr", None)
                if target_name:
                    return target_name
        cursor = parent

    # A `FormatFact(...)` / `Directive(...)` construction names its own identity
    # in a `fact_id` (or `key`) keyword. Falling back to `text(...)` — which is
    # what the first version returned for every FormatFact in the matrix — names
    # the ARGUMENT rather than the thing, and a finding that names the argument
    # cannot be acted on.
    if call_node is not None:
        for keyword in call_node.keywords:
            if keyword.arg in ("fact_id", "key", "candidate_id"):
                for sub in ast.walk(keyword.value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        return sub.value
    return "<module>"


def variant_constants(
    modules: Optional[Sequence[str]] = None,
) -> Tuple[VariantRow, ...]:
    """Return every prompt variant in the tree, derived by AST."""
    rows: List[VariantRow] = []
    for rel in tuple(modules or PROMPT_MODULES):
        for name, line, value in _string_constants(rel):
            if len(value) < 24:
                # A 24-character threshold, and it is here to exclude format
                # markers like "[neo-reminder]" rather than to judge importance.
                # It is a floor on SIZE, not a filter on meaning: nothing is
                # excluded for what it says.
                continue
            rows.append(
                VariantRow(
                    module=rel,
                    constant=name,
                    line=line,
                    chars=len(value),
                )
            )
    rows.sort(key=lambda row: (row.module, row.constant, row.line))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------


def _classify(text: str) -> List[Finding]:
    """Classify one variant's text into the closed three-way vocabulary."""
    found: List[Finding] = []
    lowered = text.lower()
    for phrase, why in CAPABILITY_CLAIM_PHRASES:
        if phrase in lowered:
            found.append(
                Finding(
                    module="",
                    constant="",
                    line=0,
                    phrase=phrase,
                    classification="capability_claim",
                    why=why,
                )
            )
    for phrase, label, convert_to in BEHAVIOURAL_PHRASES:
        if phrase in lowered:
            found.append(
                Finding(
                    module="",
                    constant="",
                    line=0,
                    phrase=phrase,
                    classification="behavioural",
                    why=f"behavioural aspiration: {label}",
                    convert_to=convert_to,
                )
            )
    return found


def scan_module(
    rel: str, modules: Optional[Sequence[str]] = None
) -> Tuple[Tuple[VariantRow, ...], Tuple[Finding, ...]]:
    """Return one module's variants and its findings."""
    rows: List[VariantRow] = []
    found: List[Finding] = []
    for name, line, value in _string_constants(rel):
        if len(value) < 24:
            continue
        hits = _classify(value)
        classification = (
            "capability_claim"
            if any(h.classification == "capability_claim" for h in hits)
            else (
                "behavioural"
                if any(h.classification == "behavioural" for h in hits)
                else "format_fact"
            )
        )
        rows.append(
            VariantRow(
                module=rel,
                constant=name,
                line=line,
                chars=len(value),
                classification=classification,
            )
        )
        for hit in hits:
            found.append(
                Finding(
                    module=rel,
                    constant=name,
                    line=line,
                    phrase=hit.phrase,
                    classification=hit.classification,
                    why=hit.why,
                    convert_to=hit.convert_to,
                )
            )
    rows.sort(key=lambda row: (row.constant, row.line))
    return tuple(rows), tuple(found)


def findings(
    modules: Optional[Sequence[str]] = None,
) -> Tuple[Finding, ...]:
    """Return every finding across every prompt module."""
    out: List[Finding] = []
    for rel in tuple(modules or PROMPT_MODULES):
        _rows, hits = scan_module(rel)
        out.extend(hits)
    return tuple(out)


def enumeration_table(
    modules: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Return the flat per-variant table a reviewer reads."""
    rows: List[VariantRow] = []
    for rel in tuple(modules or PROMPT_MODULES):
        scanned, _hits = scan_module(rel)
        rows.extend(scanned)
    rows.sort(key=lambda row: (row.module, row.constant, row.line))
    return [row.to_dict() for row in rows]


# ---------------------------------------------------------------------------
# 3. Assertion 1 — a prompt module cannot REACH a capability source
# ---------------------------------------------------------------------------

#: The names a prompt module must not import. Each is a capability authority.
#:
#: The rule is NEGATIVE and therefore asymmetric on purpose: an unknown new
#: authority is not refused by this list, so the list is not sufficient. What
#: makes it sufficient is that a prompt module has no legitimate reason to
#: import any of them, so a hit is unambiguous, and the AST check below refuses
#: the whole MODULE rather than the one name.
CAPABILITY_AUTHORITIES: Tuple[str, ...] = (
    "capability_surface",
    "capability_receipt",
    "STRATEGY_WITHHELD",
    "PolicyEngine",
    "PolicyRule",
    "ApprovalGrant",
    "ToolRegistry",
    "typed_tool_specs",
    "builtin_tool_specs",
)

#: Modules exempt from the authority check, each with a WRITTEN reason. The
#: exemption set is itself asserted, so widening it is a visible edit rather
#: than a silent one.
AUTHORITY_EXEMPTIONS: Dict[str, str] = {
    # `prompt_matrix` documents the seam that keeps family resolution on the
    # SHAPE resolver rather than the effort resolver, and a docstring quoting a
    # name is not an import. The check is on IMPORTS, so this exemption is
    # belt-and-braces rather than load-bearing; it is kept so the exemption list
    # is non-empty and therefore its own pin is non-vacuous.
    "harness/prompt_matrix.py": (
        "documents the vision_family_for seam; the check is on imports and this "
        "module imports none of the authorities"
    ),
}


def authority_findings_in_source(module: str, source: str) -> List[Dict[str, Any]]:
    """Return every way ``source`` reaches a capability authority.

    Split out from :func:`capability_widening_possible` so a pin can drive the
    **real** predicate over synthetic offenders. A pin that re-implements the
    predicate it is testing is testing its own copy, which is how a checker and
    its test drift apart while both stay green.

    Two rules, and the second exists because the first has a real hole:

    1. **no import names an authority**;
    2. **no executable code NAMES an authority at all** — because
       ``from harness.agent_kernel import kernel`` followed by
       ``kernel.capability_surface()`` brings the authority fully into scope
       while no import mentions it.

    Docstrings are excluded from rule 2, so this module's own prose about the
    authorities does not trip it.
    """
    problems: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            {"kind": "module_unparseable", "module": module, "detail": str(exc)}
        ]

    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
            names.append(str(node.module or ""))
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            for authority in CAPABILITY_AUTHORITIES:
                if authority and authority in name:
                    problems.append(
                        {
                            "kind": "capability_authority_imported",
                            "module": module,
                            "line": int(getattr(node, "lineno", 0)),
                            "name": name,
                            "authority": authority,
                        }
                    )

    docstring_ids = _docstring_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        if id(node) in docstring_ids:
            continue
        token = getattr(node, "id", None) or getattr(node, "attr", None)
        if not token:
            continue
        for authority in CAPABILITY_AUTHORITIES:
            if token == authority:
                problems.append(
                    {
                        "kind": "capability_authority_referenced",
                        "module": module,
                        "line": int(getattr(node, "lineno", 0)),
                        "name": token,
                        "authority": authority,
                    }
                )
    return problems


def capability_widening_possible(
    modules: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Return every way a prompt module could widen a capability. Must be empty.

    Four checks, and each is structural rather than textual:

    1. **no capability-authority import** — a prompt module cannot name the
       thing that grants capability, so it cannot widen it;
    1b. **no capability-authority NAME in executable code** — the shape the
       import-only check misses entirely;
    2. **no tool list in a prompt constant** — a variant that carried tool
       names would be a schema, not a prompt;
    3. **no model-facing text produced from a tool list** — a variant's value is
       a string, never a collection of specs;
    4. **the behaviour layer renders nothing** — asserted on the FUNCTION BODY,
       so a docstring explaining the emptiness cannot satisfy it.
    """
    problems: List[Dict[str, Any]] = []
    for rel in tuple(modules or PROMPT_MODULES):
        if rel in AUTHORITY_EXEMPTIONS:
            continue
        path = _module_path(rel)
        try:
            source = path.read_text(encoding="utf-8-sig")
            ast.parse(source)
        except (OSError, SyntaxError) as exc:
            problems.append(
                {
                    "kind": "module_unreadable",
                    "module": rel,
                    "detail": str(exc),
                }
            )
            continue
        problems.extend(authority_findings_in_source(rel, source))

    # (4) The behaviour layer's render is empty, read from the parsed BODY.
    problems.extend(_behaviour_render_is_empty())
    return problems


def _behaviour_render_is_empty() -> List[Dict[str, Any]]:
    """Assert ``BehaviourPolicy.render`` returns a literal empty string.

    Read from the parsed function body rather than from the source text, so the
    docstring — which explains at length why the method is empty — cannot satisfy
    the check. A method that returned a variable, a concatenation, or a
    ``policy.render(x)`` call would fail here.
    """
    from harness import behaviour_policy

    problems: List[Dict[str, Any]] = []
    try:
        source = inspect_source(behaviour_policy)
        tree = ast.parse(source)
    except (OSError, SyntaxError, TypeError) as exc:  # pragma: no cover
        return [
            {
                "kind": "behaviour_policy_unreadable",
                "module": "harness/behaviour_policy.py",
                "detail": str(exc),
            }
        ]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "render":
            body = [
                stmt
                for stmt in node.body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
            if len(body) != 1 or not isinstance(body[0], ast.Return):
                problems.append(
                    {
                        "kind": "behaviour_render_is_not_a_literal",
                        "module": "harness/behaviour_policy.py",
                        "line": int(node.lineno),
                        "detail": "render() must be exactly `return \"\"`",
                    }
                )
                continue
            value = body[0].value
            if not (isinstance(value, ast.Constant) and value.value == ""):
                problems.append(
                    {
                        "kind": "behaviour_render_is_not_empty",
                        "module": "harness/behaviour_policy.py",
                        "line": int(node.lineno),
                        "detail": repr(getattr(value, "value", "<non-literal>")),
                    }
                )
    return problems


def _docstring_constants(tree: ast.AST) -> set:
    """Return the ids of every docstring Constant node in a parse tree.

    Docstrings are excluded from the name check for the same reason they are
    excluded from the phrase scan: this module's own docstring quotes the
    authority names it refuses, and a scanner that flags its own prose is a
    scanner whose output has to be filtered before it can be read.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def inspect_source(module: Any) -> str:
    """Return a module's source. Split out so the AST checks are testable."""
    import inspect

    return inspect.getsource(module)


# ---------------------------------------------------------------------------
# 4. Assertion 2 — permission modes do not vary with the variant
# ---------------------------------------------------------------------------

#: The permission modes, by strategy name. These are the kernel's own names, so
#: a mode added to ``STRATEGY_WITHHELD`` without being listed here is a gap the
#: table makes visible rather than a silent omission.
PERMISSION_MODES: Tuple[str, ...] = (
    "daily",
    "agent_step",
    "verified_fix",
    "planning",
    "question",
    "research",
)


def modes_are_identical_across_variants(
    configs: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return whether every permission mode's receipt is invariant across variants.

    **What "identical" means here, precisely:** the capability receipt for a mode
    must not change when the prompt variant changes. A prompt variant is a
    prompt; a permission is a policy decision. If the two were ever coupled, a
    prompt edit would silently move a gate, and the evidence for it would be a
    diff of prose.

    The default configs are the three prompt layers' own keys, so the check runs
    over the configurations that actually exist rather than over a synthetic
    shape nobody ships.
    """
    from harness.agent_kernel.kernel import capability_receipt

    variants: List[Mapping[str, Any]] = list(
        configs
        if configs is not None
        else (
            {},
            {"prompt_format_facts": "anthropic"},
            {"prompt_format_facts": "openai"},
            {"prompt_format_facts": "google"},
            {"tool_deferral": True},
            {"tool_deferral": True, "tool_deferral_threshold": 0.2},
            {"skills_progressive": True},
        )
    )
    divergences: List[Dict[str, Any]] = []
    per_mode: Dict[str, List[str]] = {}
    for mode in PERMISSION_MODES:
        receipts = []
        for variant in variants:
            receipt = capability_receipt(mode, dict(variant))
            receipts.append(
                json.dumps(
                    {
                        "capabilities": receipt["capabilities"],
                        "withheld": receipt["withheld"],
                        "allowed_tools": receipt["allowed_tools"],
                    },
                    sort_keys=True,
                )
            )
        unique = sorted(set(receipts))
        per_mode[mode] = [hashlib_sha(text) for text in unique]
        if len(unique) > 1:
            divergences.append(
                {
                    "mode": mode,
                    "distinct_receipts": len(unique),
                    "variants": [dict(v) for v in variants],
                }
            )
    return {
        "modes": list(PERMISSION_MODES),
        "variants_checked": len(variants),
        "mode_digests": per_mode,
        "divergences": divergences,
        "identical": not divergences,
    }


def hashlib_sha(text: str) -> str:
    """Return a short digest, so a divergence receipt names WHICH variant."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 5. Assertion 3 — the verdict gate is unreachable from a prompt
# ---------------------------------------------------------------------------

#: The status whose mint is Phase 2's invariant.
VERDICT_STATUS = "completed_verified"


def verdict_gate_untouched(
    modules: Optional[Sequence[str]] = None,
    *,
    expected_mint_sites: Optional[int] = None,
) -> Dict[str, Any]:
    """Return whether any prompt module can reach the verdict gate.

    **The mint count is DELEGATED, not re-derived, and the reason is the third
    defect this module found in its own first draft.**

    The first version re-derived the mint sites by AST, looking for the literal
    ``"completed_verified"``. Two things were wrong with that and both would
    have produced a confident wrong number:

    * it **missed the real mint form**. The production sites assign
      ``CompletionStatus.COMPLETED_VERIFIED.value`` — an enum attribute — so the
      raw string does not appear in them at all, and the re-derivation found
      **one** site where the tree has three;
    * it **counted itself**. This module holds ``VERDICT_STATUS =
      "completed_verified"`` as its own lookup constant, so the scanner reported
      its own lookup table as a mint site. A scanner that measures itself is the
      oldest vacuity in this repository and it survived here anyway.

    So the count now comes from
    :mod:`harness.test_completion_mint_sites`, which is the existing authority
    and which already has the correct AST classifier
    (``_is_completion_status_attr``). One authority per concern, imported rather
    than forked — the lesson this tree has paid for repeatedly, and the reason
    :data:`harness.deferred_tools._estimate` delegates its token estimate for the
    same reason.

    What this module adds on top is the assertion that actually matters here:
    **no prompt module mentions the verdict at all**, in either form, so there is
    no path from a prompt string to a completion decision.
    """
    from harness.test_completion_mint_sites import (
        MINT_SITES_EXPECTED,
        enumerate_occurrences,
    )

    prompt_rels = set(modules or PROMPT_MODULES)
    occurrences = enumerate_occurrences()
    mints = [row for row in occurrences if getattr(row, "kind", "") == "mint"]
    mint_files = sorted(
        {
            str(getattr(row, "path", getattr(row, "file", "")))
            for row in mints
        }
    )

    prompt_status_hits: List[Dict[str, Any]] = []
    unparseable: List[str] = []
    for rel in sorted(prompt_rels):
        path = _module_path(rel)
        try:
            source = path.read_text(encoding="utf-8-sig")
            ast.parse(source)
        except (OSError, SyntaxError) as exc:
            # REPORTED, never skipped: a scanner that skips an unparseable file
            # reports it clean, and this repository has been bitten by exactly
            # that vacuity three times.
            unparseable.append(f"{rel}: {exc}")
            continue
        for form, needle in (
            ("string_literal", f'"{VERDICT_STATUS}"'),
            ("string_literal_single", f"'{VERDICT_STATUS}'"),
            ("enum_member", "COMPLETED_VERIFIED"),
        ):
            if needle in source:
                prompt_status_hits.append(
                    {"module": rel, "form": form, "needle": needle}
                )

    measured = int(expected_mint_sites if expected_mint_sites is not None else MINT_SITES_EXPECTED)
    return {
        "status": VERDICT_STATUS,
        "authority": "harness.test_completion_mint_sites",
        "authority_expected": int(MINT_SITES_EXPECTED),
        "prompt_modules_reaching_it": prompt_status_hits,
        "mint_site_count": len(mints),
        "mint_site_files": mint_files,
        "expected_mint_sites": measured,
        "count_unchanged": len(mints) == measured,
        "no_prompt_module_is_a_mint_site": not (set(mint_files) & prompt_rels),
        "untouched": not prompt_status_hits and not (set(mint_files) & prompt_rels),
        "unparseable_modules": unparseable,
    }


# ---------------------------------------------------------------------------
# 6. The report
# ---------------------------------------------------------------------------


def scan(
    *,
    modules: Optional[Sequence[str]] = None,
    expected_mint_sites: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the whole W2.4 scan and return its verdict."""
    rels = tuple(modules or PROMPT_MODULES)
    table = enumeration_table(rels)
    hits = findings(rels)
    widening = capability_widening_possible(rels)
    modes = modes_are_identical_across_variants()
    gate = verdict_gate_untouched(rels, expected_mint_sites=expected_mint_sites)

    capability_claims = [h for h in hits if h.classification == "capability_claim"]
    aspirations = [h for h in hits if h.classification == "behavioural"]

    problems: List[Dict[str, Any]] = []
    problems.extend(widening)
    if capability_claims:
        problems.append(
            {
                "kind": "capability_claim_in_a_prompt",
                "count": len(capability_claims),
                "names": sorted({f"{h.module}:{h.constant}" for h in capability_claims}),
            }
        )
    if not modes["identical"]:
        problems.append({"kind": "permission_mode_varies_with_the_variant",
                         "detail": modes["divergences"]})
    if not gate["untouched"]:
        problems.append({"kind": "verdict_gate_reachable_from_a_prompt",
                         "detail": gate["prompt_modules_reaching_it"]})
    if not gate["count_unchanged"]:
        problems.append(
            {
                "kind": "verdict_mint_count_moved",
                "measured": gate["mint_site_count"],
                "expected": gate["expected_mint_sites"],
            }
        )
    if gate["unparseable_modules"]:
        problems.append(
            {"kind": "unparseable_module_not_scanned",
             "detail": gate["unparseable_modules"]}
        )

    return {
        "verdict_schema": 1,
        "variants": table,
        "variant_count": len(table),
        # Attribution is a MEASUREMENT, not an assumption. Strings inside
        # function bodies (local format markers) have no module-level name, and
        # a table that silently reported them as attributed would be lying about
        # its own coverage — so the count is published and a reader can see how
        # much of the scan is named.
        "unattributed_count": sum(
            1 for row in table if row["constant"] == "<module>"
        ),
        "class_counts": {
            name: sum(1 for row in table if row["classification"] == name)
            for name in CLASSES
        },
        "findings": [h.to_dict() for h in hits],
        "capability_claims": [h.to_dict() for h in capability_claims],
        "behavioural_aspirations_for_conversion": [
            h.to_dict() for h in aspirations
        ],
        "capability_widening_possible": widening,
        "permission_modes": modes,
        "verdict_gate": gate,
        "problems": problems,
        "verdict": "PASS" if not problems else "FAIL",
        "model_behaviour_measured": False,
    }


def render_report(report: Mapping[str, Any]) -> str:
    """Render the W2.4 scan as plain text."""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("W2.4  PROMPT-VARIANT SCAN: can a prompt widen a capability?")
    lines.append("=" * 78)
    lines.append(
        f"variants scanned: {report.get('variant_count')} across "
        f"{len(PROMPT_MODULES)} modules (enumerated by AST, not hand-kept)"
    )
    counts = report.get("class_counts") or {}
    lines.append(
        "classification: "
        + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    lines.append("")
    lines.append("PER VARIANT")
    lines.append(f"  {'module':28} {'constant':30} {'class':17} chars")
    for row in report.get("variants", []):
        lines.append(
            f"  {row.get('module')!s:28} {row.get('constant')!s:30} "
            f"{row.get('classification')!s:17} {row.get('chars')}"
        )
    lines.append("")
    lines.append("CAPABILITY CLAIMS  (a finding)")
    claims = report.get("capability_claims") or []
    if not claims:
        lines.append("  none")
    for hit in claims:
        lines.append(
            f"  {hit['module']}:{hit['constant']}:{hit['line']}  {hit['phrase']!r}"
        )
        lines.append(f"      why: {hit['why']}")
    lines.append("")
    lines.append("BEHAVIOURAL ASPIRATIONS  (listed for conversion, NOT deleted)")
    aspir = report.get("behavioural_aspirations_for_conversion") or []
    if not aspir:
        lines.append("  none")
    for hit in aspir:
        lines.append(
            f"  {hit['module']}:{hit['constant']}:{hit['line']}  {hit['phrase']!r}"
        )
        lines.append(f"      convert to: {hit['convert_to']}")
    lines.append("")
    lines.append("STRUCTURAL ASSERTIONS")
    widening = report.get("capability_widening_possible") or []
    lines.append(
        f"  a prompt can widen a capability        "
        f"{'POSSIBLE' if widening else 'NO'}  ({len(widening)} findings)"
    )
    modes = report.get("permission_modes") or {}
    lines.append(
        f"  permission modes invariant             "
        f"{'YES' if modes.get('identical') else 'NO'}  "
        f"({modes.get('variants_checked')} variants x {len(modes.get('modes') or [])} modes)"
    )
    gate = report.get("verdict_gate") or {}
    lines.append(
        f"  verdict gate reachable from a prompt   "
        f"{'YES' if not gate.get('untouched') else 'NO'}"
    )
    lines.append(
        f"  verdict mint sites                    "
        f"{gate.get('mint_site_count')} measured / {gate.get('expected_mint_sites')} "
        f"expected  {gate.get('mint_site_files')}"
    )
    lines.append("")
    lines.append(f"VERDICT: {report.get('verdict')}")
    for problem in report.get("problems") or []:
        lines.append(f"  PROBLEM: {problem.get('kind')}")
    return "\n".join(lines)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the W2.4 report. Exit 0 on PASS, 2 on FAIL."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--expected-mint-sites", type=int, default=None,
                        help="default: the authority harness.test_completion_mint_sites.MINT_SITES_EXPECTED")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = scan(expected_mint_sites=args.expected_mint_sites)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_report(report))
    return 0 if report.get("verdict") == "PASS" else 2


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(_main())
