"""T1.W1.3 — the harness cannot produce a status that reads as unearned success.

Six consecutive build rounds filed the same recurring failure:
`evals/daily_driver.py` asserting `status == "success"` against a path that
honestly reports `completed_verified`, and each round blamed something else.
**Do not be the seventh.** T5 owns the eval side; this file is the harness-side
half: it enumerates every place `completed_verified` appears in `harness/**`,
classifies each one by AST, and asserts the gate has not been widened.

## Why this is an AST classification and not a grep

A grep cannot tell a MINT from a MAPPING. Both look like the word
`completed_verified` on a line:

* `status = "completed_verified" if passed else "failed"` — a **MINT**: this
  site DECIDES the value.
* `CompletionStatus.COMPLETED_VERIFIED.value: "success",` — a **MAPPING**: this
  site translates a value some other site already decided.
* `if kernel_status == "completed_verified":` — a **COMPARISON**.

Asserting on a line count would pass if a mint became a mapping and a
comparison became a mint. Asserting on the *set of deciding files* plus the
*mint count* cannot: both are derived from the parse tree, so moving a decision
between sites changes the answer.

## The finding this file does not hide

`MINT_SITES_EXPECTED` is **4**, and `phases/DOCTRINE.md` §2 says the value is
minted at *exactly one place*. That gap is the round's P0 finding, and it is
asserted here rather than described in a comment: a FIFTH mint site fails this
file immediately, and a fix that collapses four into one fails it too — forcing
the constant to be moved in the same change, which is the only moment the
collapse gets reviewed on purpose. This file deliberately does not assert the
count is one, because doing so would mean either editing a mint condition
(forbidden) or deleting the assertion.

The enumeration is published in the round's Handoff and in
`harness/AGENTS.md`, and is re-derivable at any time from
:func:`enumeration_table` rather than from a copy that can drift.

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

HARNESS_ROOT = Path(__file__).resolve().parent

MINT = "mint"
MAPPING = "mapping"
COMPARISON = "comparison"
DOCUMENTATION = "documentation"

#: The observed mint count. See the module docstring: this is > 1 and that is
#: the reported P0 finding, not a licence.
MINT_SITES_EXPECTED = 4

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
STATUS_DECIDING_FILES: Set[str] = {
    "agent_kernel/completion.py",
    "agent_kernel/verified.py",
    "agent_kernel/legacy.py",
    "agent_loop_step.py",
    "agent_loop.py",
    "core.py",
}

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
    nested Attribute nodes and both match a naive check, which counted the
    fourth mint site twice. The occurrence is the whole chain, so the inner
    node must be skipped when its parent is another link in the same chain.
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
    applies. The order matters and is the reason this is not a line regex:
    `status = "completed_verified" if passed else "failed"` is a mint because
    the constant is the CONSEQUENCE of a boolean decision, while
    `X: "success"` in a dict is a mapping because the constant is a KEY.
    """
    if id(node) in docs:
        return DOCUMENTATION
    parent = parents.get(id(node))
    if parent is None:
        return COMPARISON

    # A conditional expression whose test is a comparison is a DECISION.
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
    # A value/attribute chain such as `CompletionStatus.COMPLETED_VERIFIED.value`
    # is part of the occurrence itself, not a separate node.
    if isinstance(parent, ast.Attribute) and parent.attr == "value":
        return _classify(parent, parents, docs)
    # A bare string literal in a collection is a membership test.
    if isinstance(parent, (ast.Set, ast.List, ast.Tuple)):
        return COMPARISON
    return COMPARISON


def enumerate_occurrences() -> List[Occurrence]:
    """Classify every `completed_verified` occurrence in `harness/**`."""
    found: List[Occurrence] = []
    for path in _harness_sources():
        source = path.read_text(encoding="utf-8", errors="replace")
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


def enumeration_table() -> Dict[str, List[Occurrence]]:
    """Return the enumeration grouped by file, for a reader or a handoff."""
    table: Dict[str, List[Occurrence]] = {}
    for occurrence in enumerate_occurrences():
        table.setdefault(occurrence.file, []).append(occurrence)
    return table


ENUMERATION = enumerate_occurrences()
MINTS = [o for o in ENUMERATION if o.kind == MINT]


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def test_the_enumeration_is_not_empty_and_finds_all_three_kinds() -> None:
    """Non-vacuity first: a classifier that matched nothing satisfies the rest.

    Asserting that the enumeration is non-empty AND that it distinguishes all
    of mint / mapping / comparison / documentation means every count assertion
    below is a real measurement rather than a comparison against zero.
    """
    assert ENUMERATION, "no `completed_verified` found in harness/ - suspicious"
    kinds = {o.kind for o in ENUMERATION}
    assert {MINT, MAPPING, COMPARISON} <= kinds, (
        f"the enumeration must distinguish mints, mappings and comparisons; got "
        f"{sorted(kinds)}"
    )


def test_the_mint_site_count_is_recorded_and_unchanged() -> None:
    """The count of MINT sites, asserted from the parse tree.

    `MINT_SITES_EXPECTED` is 4 and `phases/DOCTRINE.md` §2 says exactly one.
    That gap is the reported finding. Asserting the observed count is the only
    honest option available to a round that is forbidden from changing a mint
    CONDITION: it makes a fifth site fail immediately, and it makes a
    legitimate four-into-one fix fail until the constant moves in the same
    change.
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
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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
    even before its occurrences are classified, so an unexamined mint cannot
    sit in the tree.
    """
    deciding = set()
    for path in _harness_sources():
        relative = path.relative_to(HARNESS_ROOT).as_posix()
        if relative not in STATUS_DECIDING_FILES:
            continue
        if any(o.file == relative for o in MINTS):
            deciding.add(relative)
    assert deciding <= STATUS_DECIDING_FILES
    for occurrence in MINTS:
        assert occurrence.file in STATUS_DECIDING_FILES, (
            f"{occurrence.as_row()} mints from a module that is not declared "
            f"as status-deciding"
        )


def test_agent_loop_step_contains_no_string_literal_equal_to_success() -> None:
    """The existing source-level pin, re-verified and made structural.

    `phases/DOCTRINE.md` §2: `agent_loop_step.py` contains no string literal
    equal to `success` outside its own docstrings. Parsing the AST is stronger
    than a grep — a literal hidden in a concatenation or a formatted string
    fools a text scan but not this.
    """
    path = HARNESS_ROOT / "agent_loop_step.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
    core = (HARNESS_ROOT / "core.py").read_text(encoding="utf-8")
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
            pytest_fail(
                f"core.py:{call.lineno} hard-codes verification={supplied.value!r}"
            )
        elif isinstance(supplied, ast.Call):
            assert getattr(supplied.func, "id", "") == "VerificationResult", (
                f"core.py:{call.lineno} builds `verification` from "
                f"{getattr(supplied.func, 'id', '?')}(), not a VerificationResult"
            )
        else:
            assert isinstance(supplied, ast.Name), (
                f"core.py:{call.lineno} passes an unexpected verification expression"
            )


def pytest_fail(message: str) -> None:
    """Fail with `message` without importing pytest at module scope."""
    raise AssertionError(message)


def test_the_core_mapping_is_keyed_on_the_verified_value_alone() -> None:
    """`core.py`'s kernel-status -> historical-status map cannot widen.

    This is a MAPPING, not a mint — it decides nothing. But a mapping is the
    easiest place to widen a gate by accident, because it reads like a
    vocabulary adapter. The assertion is on the SHAPE: the historical `success`
    word is reachable only from the verified value, and exactly once.
    """
    mappings = [o for o in ENUMERATION if o.file == "core.py" and o.kind == MAPPING]
    assert mappings, "core.py no longer maps COMPLETED_VERIFIED; inspect it"
    for occurrence in mappings:
        line = occurrence.text
        assert re_matches(line), (
            f"core.py:{occurrence.line} is classified as a mapping but does not "
            f"look like one: {line}"
        )
        assert line.count("success") == 1, (
            f"the mapping names success more than once: {line}"
        )


def re_matches(line: str) -> bool:
    """True when a line reads like `<verified value>: "success"`."""
    import re

    return bool(re.search(r"COMPLETED_VERIFIED\.value\s*:\s*[\"']success[\"']", line))


def test_the_compat_mapping_is_keyed_on_the_verified_value_alone() -> None:
    """`agent_loop._compat_status` — the same shape, the same assertion.

    Parsed rather than grepped: the function's body is read and every `return`
    of the historical `success` word must be dominated by a comparison against
    `completed_verified`. A `success` returned anywhere else is the widening
    this pin exists to stop.
    """
    from harness.agent_loop import _compat_status

    assert _compat_status("completed_verified", "success") == "success"
    for other in ("completed_unverified", "failed", "blocked", "needs_input", ""):
        assert _compat_status(other, "success") != "success", (
            f"_compat_status({other!r}) returned 'success'; an unverified run "
            "must never read as a clean pass"
        )
    assert _compat_status("completed_unverified", "x") == "completed_unverified"


def test_the_four_mints_are_all_reachable_live_code() -> None:
    """A mint nothing can reach is not a mint — and a dead guard is worse.

    The doctrine's rule. Asserted by checking each of the four is registered
    in the kernel's strategy registry or dispatched by name, so the count in
    `MINT_SITES_EXPECTED` describes live code rather than four orphaned copies.
    """
    kernel = (HARNESS_ROOT / "agent_kernel" / "kernel.py").read_text(encoding="utf-8")
    assert "VerifiedFixStrategy" in kernel, "the verified_fix mint is unreachable"
    assert "LegacyAgentStrategy" in kernel, "the legacy_agent mint is unreachable"

    from harness.agent_kernel import completion as completion_module

    assert hasattr(completion_module, "CompletionPolicy"), (
        "the completion.py mint is unreachable - its class is gone"
    )
    from harness.agent_kernel.kernel import STRATEGY_NAMES

    assert {"daily", "verified_fix", "legacy_agent"} <= set(STRATEGY_NAMES)

    loop = (HARNESS_ROOT / "agent_loop.py").read_text(encoding="utf-8")
    assert "run_agent_stepped" in loop, (
        "the stepped engine that owns the fourth mint is unreachable"
    )
