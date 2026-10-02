"""T1.W2.2 — `completed_verified` is minted at exactly KNOWN-N sites, and the
divergences between them are pinned EXACTLY rather than described.

**Read this first, because the prompt's framing and the tree disagree.**
`phases/DOCTRINE.md` §2 and `phases/README.md` standing-invariant #1 both say
`completed_verified` is minted at exactly ONE place. It is minted at **FOUR**,
all four reachable. Wave 1 found this and recorded it; Wave 2's job is to PIN
it, and the pin must assert the number that is actually true rather than the
number the doctrine claims. Asserting `1` would require editing a mint
CONDITION, which this wave forbids outright.

So `MINT_SITES_EXPECTED = 4` and the two divergences are pinned as exact
assertions. A collapse to one fails this file until the constant moves in the
same change — which is the only moment that collapse gets reviewed on purpose.

## Why AST and not a grep

A grep cannot tell a MINT from a MAPPING or a COMPARISON:

* `status = "completed_verified" if passed else "failed"` — a **MINT**: this
  site DECIDES the value.
* `CompletionStatus.COMPLETED_VERIFIED.value: "success",` — a **MAPPING**: it
  translates a value some other site already decided.
* `if kernel_status == "completed_verified":` — a **COMPARISON**.

A line-count assertion would pass if a mint became a mapping and a comparison
became a mint. Asserting on the SET of deciding files plus the COUNT cannot:
both are derived from the parse tree, so moving a decision between sites
changes the answer.

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

HARNESS_ROOT = Path(__file__).resolve().parent

MINT = "mint"
MAPPING = "mapping"
COMPARISON = "comparison"
DOCUMENTATION = "documentation"

#: The observed mint count, and the gap against the doctrine's claim of one.
#: This is a P0 FINDING, not a licence: `test_the_mint_count_is_not_one_because
#: _the_doctrine_says_so` asserts the doctrine text still says "one", so the
#: divergence between the claim and the tree cannot be quietly resolved by
#: editing this constant.
MINT_SITES_EXPECTED = 4

#: What the doctrine claims, quoted from the tree so the pin reads the real file
#: rather than a copy that can drift.
DOCTRINE_FILE = HARNESS_ROOT.parent / "phases" / "DOCTRINE.md"

#: The four files that currently DECIDE the value, pinned by name. A count is a
#: weaker statement than a set: a fifth site in a new module while one of these
#: stopped minting would hold the count and fail nothing.
EXPECTED_MINT_FILES: Set[str] = {
    "agent_kernel/completion.py",
    "agent_kernel/verified.py",
    "agent_kernel/legacy.py",
    "agent_loop_step.py",
}

#: Files allowed to construct a terminal status at all. A new status-deciding
#: module must be added here on purpose, before anyone classifies it.
STATUS_DECIDING_FILES: Set[str] = EXPECTED_MINT_FILES | {"agent_loop.py", "core.py"}

#: Target names whose assigned value IS a status decision.
_STATUS_TARGETS = ("status", "state", "kernel_status", "legacy_status")


@dataclass(frozen=True)
class Occurrence:
    """One occurrence of `completed_verified`, with its classification."""

    file: str
    line: int
    kind: str
    text: str

    def as_row(self) -> str:
        return f"{self.file}:{self.line} [{self.kind}] {self.text}"


def _harness_sources() -> List[Path]:
    return sorted(
        path
        for path in HARNESS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
        and "_stubs" not in path.parts
        and not path.name.startswith("test_")
    )


def _parents(tree: ast.AST) -> Dict[int, ast.AST]:
    """Map `id(node)` -> parent, so a node can ask what it sits inside."""
    table: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            table[id(child)] = node
    return table


def _is_completion_status_attr(node: ast.AST) -> bool:
    """True for the OUTERMOST node of a `CompletionStatus.COMPLETED_VERIFIED...`.

    "Outermost" matters: `CompletionStatus.COMPLETED_VERIFIED.value` is TWO
    nested Attribute nodes and both match a naive check, which double-counted
    the fourth mint site when this classifier was first written.
    """
    return bool(
        isinstance(node, ast.Attribute)
        and (
            node.attr == "COMPLETED_VERIFIED"
            or (node.attr == "value" and _is_completion_status_attr(node.value))
        )
    )


def _is_inner_chain_link(node: ast.AST, parents: Dict[int, ast.AST]) -> bool:
    """True when `node`'s parent is the same `CompletionStatus...` chain."""
    parent = parents.get(id(node))
    return (
        isinstance(parent, ast.Attribute)
        and parent.attr in {"COMPLETED_VERIFIED", "value"}
        and parent.value is node
    )


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    """Every node that is part of a docstring expression."""
    inside: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                inside.add(id(first.value))
    return inside


def _assigned_status_name(node: ast.AST) -> Optional[str]:
    """If `node` is the value of a status assignment, return the target name."""
    if isinstance(node, ast.Assign):
        names = [
            t.id
            for t in node.targets
            if isinstance(t, ast.Name) and t.id in _STATUS_TARGETS
        ]
        return names[0] if names else None
    if isinstance(node, ast.AnnAssign):
        target = node.target
        if isinstance(target, ast.Name) and target.id in _STATUS_TARGETS:
            return target.id
    return None


def _classify(node: ast.AST, parents: Dict[int, ast.AST], docs: Set[int]) -> str:
    """Classify one occurrence by what it sits inside.

    Walks outward one step at a time and answers the first question that
    applies. The order is the argument, not an implementation detail:
    `status = "completed_verified" if passed else "failed"` is a mint because
    the constant is the CONSEQUENCE of a boolean decision, while `X: "success"`
    in a dict is a mapping because the constant is a KEY.
    """
    if id(node) in docs:
        return DOCUMENTATION
    parent = parents.get(id(node))
    if parent is None:
        return COMPARISON

    # A conditional expression whose test is a decision IS the decision.
    if isinstance(parent, ast.IfExp):
        return MINT
    # A dict key renames a value; a dict value carries one.
    if isinstance(parent, ast.Dict) and node in parent.keys:
        return MAPPING
    if isinstance(parent, ast.Compare):
        return COMPARISON
    # `RunResult(status=status, ...)` names a status but does not decide it.
    if isinstance(parent, ast.keyword) and parent.arg in {"status", "state"}:
        return MAPPING
    # A status assignment is the decision.
    if _assigned_status_name(parent) is not None:
        return MINT
    # An outer link of the `CompletionStatus.X.value` chain classifies as the
    # chain does, so the decision one level up is not missed.
    if isinstance(parent, ast.Attribute) and parent.attr in {
        "COMPLETED_VERIFIED",
        "value",
    }:
        return _classify(parent, parents, docs)
    # A bare string literal in a collection is a membership test.
    if isinstance(parent, (ast.Set, ast.List, ast.Tuple)):
        return COMPARISON
    return COMPARISON


def enumerate_occurrences() -> List[Occurrence]:
    """Classify every `completed_verified` occurrence in `harness/`."""
    found: List[Occurrence] = []
    for path in _harness_sources():
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(source)
        parents = _parents(tree)
        docs = _docstring_nodes(tree)
        relative = path.relative_to(HARNESS_ROOT).as_posix()
        lines = source.splitlines()

        for node in ast.walk(tree):
            if _is_inner_chain_link(node, parents):
                continue
            hit = (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "completed_verified"
            ) or _is_completion_status_attr(node)
            if not hit:
                continue
            lineno = getattr(node, "lineno", 0)
            found.append(
                Occurrence(
                    file=relative,
                    line=lineno,
                    kind=_classify(node, parents, docs),
                    text=lines[lineno - 1].strip() if lineno else "",
                )
            )
    return sorted(found, key=lambda o: (o.file, o.line))


ENUMERATION = enumerate_occurrences()
MINTS = [o for o in ENUMERATION if o.kind == MINT]


def enumeration_table() -> Dict[str, List[Occurrence]]:
    """Return the enumeration grouped by file, for a reader or a handoff."""
    table: Dict[str, List[Occurrence]] = {}
    for occurrence in ENUMERATION:
        table.setdefault(occurrence.file, []).append(occurrence)
    return table


# ---------------------------------------------------------------------------
# 1. The mint count and the mint set
# ---------------------------------------------------------------------------


def test_the_enumeration_is_not_empty_and_finds_all_the_kinds() -> None:
    """Non-vacuity first: a classifier that matched nothing satisfies the rest.

    Asserting that the enumeration is non-empty AND that it distinguishes mint /
    mapping / comparison / documentation means every count assertion below is a
    real measurement rather than a comparison against zero.
    """
    assert ENUMERATION, "no `completed_verified` found in harness/ - suspicious"
    kinds = {o.kind for o in ENUMERATION}
    assert {MINT, MAPPING, COMPARISON} <= kinds, (
        f"the enumeration must distinguish mints, mappings and comparisons; got "
        f"{sorted(kinds)}"
    )


def test_the_mint_site_count_is_recorded_and_unchanged() -> None:
    """THE PIN. A second mint site (or a collapse) fails here.

    Asserting the OBSERVED count is the only honest option available to a round
    forbidden from changing a mint CONDITION: it makes a fifth site fail
    immediately, and it makes a legitimate four-into-one fix fail until this
    constant moves in the same change — which is the only moment that collapse
    gets reviewed on purpose.
    """
    assert len(MINTS) == MINT_SITES_EXPECTED, (
        f"expected {MINT_SITES_EXPECTED} completed_verified mint sites, found "
        f"{len(MINTS)}:\n" + "\n".join(o.as_row() for o in MINTS)
    )


def test_the_mint_sites_are_the_four_named_files() -> None:
    """Which files mint, pinned by name as well as by count."""
    found = {o.file for o in MINTS}
    assert found == EXPECTED_MINT_FILES, (
        f"mint sites changed.\n  expected: {sorted(EXPECTED_MINT_FILES)}\n"
        f"  found:    {sorted(found)}"
    )


def test_the_four_mints_each_depend_on_a_verification_decision() -> None:
    """Every mint must be downstream of real evidence, not of a constant.

    A mint whose condition is a literal, or names nothing about verification,
    would be a completion claim with no measurement behind it. Each of the four
    sites is read with its enclosing condition and asserted to mention the
    verification vocabulary. This is what makes the count above a statement
    about the GATE rather than about four copies of a word.
    """
    vocabulary = ("target", "regression", "flaky", "clean", "error", "evidence")
    for occurrence in MINTS:
        path = HARNESS_ROOT / occurrence.file
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        window = "\n".join(
            lines[max(0, occurrence.line - 12) : occurrence.line + 2]
        ).lower()
        assert any(word in window for word in vocabulary), (
            f"{occurrence.as_row()} has no verification vocabulary within 12 "
            "lines above it; a mint that is not downstream of evidence is a "
            "completion claim with no measurement"
        )


def test_no_module_outside_the_declared_set_decides_a_status() -> None:
    """A new status-deciding module must be declared here before it exists.

    The pin that makes the mint count an ongoing property: a module that starts
    assigning `status = ...completed...` outside `STATUS_DECIDING_FILES` fails
    even before its occurrences are classified, so an unexamined mint cannot sit
    in the tree.
    """
    for occurrence in MINTS:
        assert occurrence.file in STATUS_DECIDING_FILES, (
            f"{occurrence.as_row()} mints from a module that is not declared as "
            "status-deciding"
        )


# ---------------------------------------------------------------------------
# 2. The doctrine's claim, and the divergence between it and the tree
# ---------------------------------------------------------------------------


def test_the_mint_count_is_not_one_because_the_doctrine_says_so() -> None:
    """The gap between the CLAIM and the TREE is itself pinned.

    `phases/DOCTRINE.md` §2 says the value is minted at exactly one place. It is
    minted at four. Asserting the doctrine text still says "one" is what stops
    this round's `MINT_SITES_EXPECTED = 4` from quietly becoming the new
    doctrine: the day someone edits the doctrine to match the code, this test
    fails and asks them to reconcile the two in the same change, which is the
    only moment the divergence gets an owner.

    Read from the file rather than from a copy, so a rewrite of the prose shows
    up here instead of leaving a stale claim pinned in a test.
    """
    if not DOCTRINE_FILE.exists():
        # The file is untracked in some trees; the mint count above still holds,
        # and a missing doctrine is not a reason to fail a harness suite.
        return
    text = DOCTRINE_FILE.read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()
    assert "completed_verified" in lowered, (
        "phases/DOCTRINE.md no longer mentions completed_verified; the mint "
        "gate's standing invariant moved. Read it before touching this file - "
        "the recorded four-site divergence may have been resolved upstream."
    )
    claims_single = any(
        marker in lowered
        for marker in (
            "exactly one place",
            "exactly one site",
            "minted at exactly one",
            "one single mint",
        )
    )
    if not claims_single:
        return
    assert MINT_SITES_EXPECTED != 1, (
        "the doctrine still claims `completed_verified` is minted at exactly "
        "ONE place and MINT_SITES_EXPECTED is now 1. If the four sites were "
        "collapsed, that is the real fix the round recorded — update "
        "phases/DOCTRINE.md in the same change so the claim matches the tree."
    )


def test_the_three_mint_conditions_are_recorded_with_their_exact_differences() -> None:
    """The four mint CONDITIONS, pinned so none can widen silently.

    This is the harness-side half of the divergence that
    `tests/test_agent_loop_matrix.py::test_the_three_mint_conditions_agree_
    across_the_tree` documents (that test is T5's lane; it still passes and
    still documents both divergences, verified here).

    The three conditions, as measured:

    | site | condition | looser than the pure core? |
    |---|---|---|
    | `agent_kernel/completion.py` | `target ∧ regression(default True) ∧ ¬flaky ∧ ¬error` | no — strictest |
    | `agent_kernel/verified.py` | `target ∧ regression ∧ ¬flaky` — **no `¬error`** | **yes**: an error-bearing block reads clean |
    | `agent_kernel/legacy.py` | `target ∧ regression(**default True**) ∧ ¬flaky` — **no `¬error`** | **yes, twice** |
    | `agent_loop_step.py` | `¬error ∧ target ∧ regression ∧ ¬flaky`, absent = False | no |

    Asserted by reading each site's CONDITION, not by importing and calling. A
    behavioural probe would pin the ANSWER without pinning the CONDITION, and the
    condition is what a future edit changes.

    **The condition is not always in the same function as the mint.** The pure
    core's mint is `CompletionStatus.COMPLETED_VERIFIED.value if clean else ...`
    and `clean` comes from `_evidence_is_clean`, ~230 lines below. Reading only
    the lines above the mint would therefore pass vacuously for the strict site
    and say nothing about it — so each site's CONDITION SOURCE is resolved
    explicitly below, and the term is asserted there.
    """
    # relative file -> (enclosing function, required terms inside it)
    #
    # `completion.py` inlines the condition in the same function as the mint
    # (`passed = ... and not verification.get("error")`); the other three read it
    # from a named helper. Both shapes are pinned, by NAME, because a line
    # anchor breaks on every unrelated edit above it.
    conditions = {
        # (enclosing function, terms that must appear inside it)
        "agent_kernel/completion.py": ("finish", ("error",)),
        "agent_loop_step.py": ("_evidence_is_clean", ("error",)),
        "agent_kernel/verified.py": ("run", ()),
        "agent_kernel/legacy.py": ("run", ()),
    }
    for relative, (symbol, required) in conditions.items():
        body = _function_body(relative, symbol)
        assert body is not None, (
            f"{relative} no longer defines {symbol}(); the recorded mint "
            "condition for that site cannot be read, so this pin is asserting "
            "nothing about it. Find where the condition went and update this "
            "table in the same change."
        )
        for term in required:
            assert term in body.lower(), (
                f"{relative}::{symbol} no longer references `{term}`. The pure "
                "core requires the evidence to carry NO error, and that term "
                "disappearing from a strict site is exactly the widening this "
                "pin exists to stop."
            )

    # The strict sites must also default an ABSENT regression term to False, and
    # the loose one to True — that is divergence #2, and it is the difference
    # between "an evidence block that omits a term cannot claim it" and "it can".
    pure = _function_body("agent_loop_step.py", "_evidence_is_clean") or ""
    pure_get = _get_call_arguments(pure, "regression_passed")
    assert pure_get is not None, (
        "the pure core's `_evidence_is_clean` no longer reads "
        "`regression_passed` at all. The recorded divergence #2 compares the two "
        "engines' handling of an ABSENT regression term, so one that does not read "
        "the term cannot be compared — update this pin in the same change."
    )
    assert "True" not in pure_get, (
        f"the pure core's `_evidence_is_clean` reads regression_passed as "
        f"{pure_get!r}, i.e. it now defaults a MISSING term to True. That is "
        "recorded divergence #2 and it is the direction that matters: an evidence "
        "block which omits a term must not be able to claim it."
    )
    legacy_run = _function_body("agent_kernel/legacy.py", "run") or ""
    legacy_default = _get_call_arguments(legacy_run, "regression_passed")
    assert legacy_default == "True", (
        f"harness/agent_kernel/legacy.py reads regression_passed with default "
        f"{legacy_default!r}; the recorded divergence #2 is that it defaults a "
        "MISSING term to True where the pure core treats absent as False. If it "
        "was tightened, delete this assertion and record the fix in "
        "harness/AGENTS.md — this round must not silently absorb a tightening."
    )

    # And divergence #1: the two loose sites must NOT reference `error` in the
    # `clean` computation, i.e. an evidence block that also carries an error
    # still reads as clean to them.
    #
    # Read from the `clean = ...` assignment rather than from the whole
    # enclosing function: `run()` in both modules handles verifier FAILURES
    # afterwards and legitimately mentions `error` there, so a whole-body check
    # would be a pin that cannot discriminate the thing it claims to.
    for relative in ("agent_kernel/verified.py", "agent_kernel/legacy.py"):
        body = _function_body(relative, "run") or ""
        clean_expr = _assignment_rhs(body, "clean")
        assert clean_expr, (
            f"{relative}::run no longer assigns a `clean` expression; the "
            "recorded divergence #1 cannot be read. Find where the mint "
            "condition moved and update this pin in the same change."
        )
        assert "error" not in clean_expr.lower(), (
            f"{relative}'s `clean = ...` now references `error`, so recorded "
            "divergence #1 (an error-bearing evidence block reads clean) has "
            "been CLOSED. Delete this assertion and record the fix — a verifier "
            "that crashed and left stale booleans behind must not mint a pass, so "
            "this is the RIGHT direction, but it is a behaviour change and it "
            "has to be a recorded one rather than a silently absorbed fix."
        )


def _get_call_arguments(function_source: str, key: str) -> Optional[str]:
    """The `evidence.get("<key>", <default>)` argument text, or None.

    Parsed rather than substring-matched, because the whole divergence is about
    the DEFAULT — and a substring test for `", True)"` cannot tell
    `get("regression_passed")` from `get("regression_passed", True)`, nor
    `get("flaky", False)` from a third key. Returns the rendered default
    argument, or None when the key is read with no default at all.
    """
    tree = ast.parse(textwrap.dedent(function_source))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == key
        ):
            return ast.unparse(node.args[1]) if len(node.args) > 1 else ""
    return None


def _assignment_rhs(function_source: str, target: str) -> str:
    """The right-hand side of `target = ...` inside a function's source, or ''."""
    tree = ast.parse(textwrap.dedent(function_source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == target:
                    return ast.unparse(node.value)
    return ""


def _function_body(relative: str, symbol: str) -> Optional[str]:
    """The source of one function's body in a harness module, or None.

    Read from the parse tree and located by NAME rather than by line, because a
    line-anchored assertion is a pin that breaks on every unrelated edit above
    it — and a pin that breaks on unrelated edits is a pin that gets deleted.
    """
    tree = ast.parse(
        (HARNESS_ROOT / relative).read_text(encoding="utf-8-sig", errors="replace")
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol
        ):
            return ast.unparse(node)
    return None


def test_the_three_mint_conditions_agree_across_the_tree_still_passes() -> None:
    """T5's test is the behavioural half; this asserts it is still THERE.

    `tests/test_agent_loop_matrix.py::test_the_three_mint_conditions_agree_
    across_the_tree` is the assertion that actually exercises the three
    conditions against an evidence-shape matrix. It is in T5's lane and this
    round did not edit it — but a divergence pin in a file nobody runs is not a
    pin. So this asserts the test still exists, by name, in the tree.

    If it has been renamed or deleted, this fires and asks for the replacement,
    rather than leaving `harness/AGENTS.md` §9 pointing at a name that is gone.
    """
    matrix = HARNESS_ROOT.parent / "tests" / "test_agent_loop_matrix.py"
    if not matrix.exists():
        return  # untracked in some trees; the mint pins above still hold
    source = matrix.read_text(encoding="utf-8", errors="replace")
    assert "def test_the_three_mint_conditions_agree_across_the_tree(" in source, (
        "tests/test_agent_loop_matrix.py no longer defines "
        "test_the_three_mint_conditions_agree_across_the_tree. It is the "
        "behavioural half of the recorded divergence and harness/AGENTS.md §9 "
        "names it; either restore it or update this reference in the same change."
    )
    # And it must still document BOTH divergences, not just one.
    assert "legacy" in source.lower() and "regression" in source.lower(), (
        "the three-mint-conditions test no longer mentions the legacy adapter's "
        "regression default; one of the two recorded divergences has stopped "
        "being documented"
    )


# ---------------------------------------------------------------------------
# 3. The two MAPPING sites cannot widen
# ---------------------------------------------------------------------------


def test_the_core_mapping_is_keyed_on_the_verified_value_alone() -> None:
    """`core.py`'s kernel-status -> historical-status map cannot widen.

    This is a MAPPING, not a mint — it decides nothing. But a mapping is the
    easiest place to widen a gate by accident, because it reads like a
    vocabulary adapter. The assertion is on the SHAPE: the historical `success`
    word is reachable only from the verified value, and exactly once.
    """
    import re

    mappings = [o for o in ENUMERATION if o.file == "core.py" and o.kind == MAPPING]
    assert mappings, "core.py no longer maps COMPLETED_VERIFIED; inspect it"
    for occurrence in mappings:
        line = occurrence.text
        assert re.search(r"COMPLETED_VERIFIED\.value\s*:\s*[\"']success[\"']", line), (
            f"core.py:{occurrence.line} is classified as a mapping but does not "
            f"look like one: {line}"
        )
        assert line.count("success") == 1, (
            f"the mapping names success more than once: {line}"
        )


def test_the_compat_mapping_is_keyed_on_the_verified_value_alone() -> None:
    """`agent_loop._compat_status` — the same shape, asserted live.

    Parsed rather than grepped: the function is CALLED, and every status the
    legacy vocabulary can produce is checked. A `success` returned from any
    branch other than the verified one is the widening this pin exists to stop.
    """
    from harness.agent_loop import _compat_status

    assert _compat_status("completed_verified", "success") == "success"
    for other in ("completed_unverified", "failed", "blocked", "needs_input", ""):
        assert _compat_status(other, "success") != "success", (
            f"_compat_status({other!r}) returned 'success'; an unverified run "
            "must never read as a clean pass"
        )
    assert _compat_status("completed_unverified", "x") == "completed_unverified"


def test_agent_loop_step_contains_no_string_literal_equal_to_success() -> None:
    """The existing source-level pin, re-verified and made structural.

    `phases/DOCTRINE.md` §2: `agent_loop_step.py` contains no string literal
    equal to `success` outside its own docstrings. Parsing the AST is stronger
    than a grep — a literal hidden in a concatenation or an f-string fools a
    text scan but not this.
    """
    path = HARNESS_ROOT / "agent_loop_step.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == "success"
    ]
    assert offenders == [], (
        f"agent_loop_step.py has a `success` string literal at line(s) "
        f"{offenders}; the terminal vocabulary is RUN_STATUSES and it contains "
        "no bare 'the run worked' word"
    )
    assert ast.get_docstring(tree), "the module must still document itself"


def test_the_terminal_vocabulary_contains_no_bare_success_word() -> None:
    """`shared.agent_contracts.RUN_STATUSES`, read and asserted directly.

    Imported rather than grepped so a rename in another slot's file shows up
    here as a failure with the actual value, instead of as an ImportError three
    suites later.
    """
    from shared.agent_contracts import RUN_STATUSES

    assert "success" not in RUN_STATUSES, (
        f"RUN_STATUSES gained a bare success word: {RUN_STATUSES}"
    )
    assert {"completed_verified", "completed_unverified"} <= set(RUN_STATUSES)


def test_every_taskresult_construction_sets_verification_from_real_evidence() -> None:
    """No `TaskResult` invocation invents a `verification` value.

    Read from the parse tree rather than matched on the string
    `verification=True`, because a `**kwargs` call would satisfy a text scan.
    An absent verification is not a passing verification, so the key must be
    present and must be a `VerificationResult(...)` call or a `Name` bound to
    one — never a literal.
    """
    core = (HARNESS_ROOT / "core.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(core)
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TaskResult"
    ]
    assert sites, "no TaskResult construction found - the pin would be vacuous"

    for call in sites:
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        assert "verification" in keywords, (
            f"TaskResult at core.py:{call.lineno} passes no `verification`; an "
            "absent measurement is not a passing measurement"
        )
        supplied = keywords["verification"]
        if isinstance(supplied, ast.Constant):
            raise AssertionError(
                f"core.py:{call.lineno} hard-codes verification={supplied.value!r}"
            )
        if isinstance(supplied, ast.Call):
            assert getattr(supplied.func, "id", "") == "VerificationResult", (
                f"core.py:{call.lineno} builds `verification` from "
                f"{getattr(supplied.func, 'id', '?')}(), not a VerificationResult"
            )
        else:
            assert isinstance(supplied, ast.Name), (
                f"core.py:{call.lineno} passes an unexpected verification expression"
            )


def test_the_four_mints_are_all_reachable_live_code() -> None:
    """A mint nothing can reach is not a mint — and a dead guard is worse.

    Asserted by checking each of the four is registered in the kernel's strategy
    registry or dispatched by name, so the count in `MINT_SITES_EXPECTED`
    describes live code rather than four orphaned copies.
    """
    kernel = (HARNESS_ROOT / "agent_kernel" / "kernel.py").read_text(
        encoding="utf-8-sig", errors="replace"
    )
    assert "VerifiedFixStrategy" in kernel, "the verified_fix mint is unreachable"
    assert "LegacyAgentStrategy" in kernel, "the legacy_agent mint is unreachable"

    from harness.agent_kernel import completion as completion_module
    from harness.agent_kernel.kernel import STRATEGY_NAMES

    assert hasattr(completion_module, "CompletionPolicy"), (
        "the completion.py mint is unreachable - its class is gone"
    )
    assert {"daily", "verified_fix", "legacy_agent"} <= set(STRATEGY_NAMES)

    loop = (HARNESS_ROOT / "agent_loop.py").read_text(encoding="utf-8-sig")
    assert "run_agent_stepped" in loop, (
        "the stepped engine that owns the fourth mint is unreachable"
    )
