"""Loop-body complexity, measured from the AST rather than counted by hand.

The claim this module exists to make falsifiable is the one a refactor round is
usually *not* able to make: *"the refactor reduced the thing it refactored."*
A line count taken by eye moves when somebody reformats a comment; a hand-kept
branch count moves when somebody forgets to update it. Both are claims, not
measurements.

So the measurement is derived from the parsed source every time it is asked for:

* ``lines`` — the loop node's own ``lineno..end_lineno`` span. Comments inside
  the body count, because they are lines a reader has to get past.
* ``statements`` — recursive ``ast.stmt`` count inside the body.
* ``branches`` — recursive ``If`` + ``IfExp`` + ``Match`` + ``Try`` + inner
  ``For`` + inner ``While``. This is the "branch count" the DoD asks for.
* ``conditionals`` — recursive ``If`` + ``IfExp`` only. A ``try`` is a branch
  in the sense that it can divert control, but it is not a *conditional*; the
  two are reported separately so a reordering of the loop cannot flatter either
  one by moving work between them.
* ``policy_branches`` — branches whose subtree mentions a term from
  ``POLICY_VOCABULARY``. **This is the number the refactor is judged on.** It is
  the count of in-loop turn-level policy decisions, and it is what R1's
  "graduate to a middleware pipeline once you need three or more independent
  turn-level policies" threshold is actually measured against.

``BASELINE`` records the measured *before* figures for both turn loops. A pin
asserts the current measurement is at or below every baseline row, which is what
turns "the loop got smaller" from a sentence in a handoff into a property that
fails on the next regression. Baseline rows are only ever tightened downward
and never widened upward without a written reason, because a baseline that can
grow is not a ratchet.

``python -m runtime.loop_complexity [--json]`` prints the table and exits **2**
if any loop is above its baseline row — the same shape as
``python -m runtime.invariants``, so a lane that runs it cannot report green
over a loop that grew.

Nothing here imports ``harness``. It reads source text and parses it, which is
the only way to measure another module's loop without depending on it.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class LoopRef:
    """A declared turn loop to measure.

    ``qualifier`` + ``function`` + ``marker`` together locate the loop node
    unambiguously. All three are needed: the class name because
    ``harness/agent_kernel/strategy.py`` declares an abstract ``AgentStrategy.run``
    *before* ``DailyCodingStrategy.run``, and a walk that matched on the
    function name alone would silently measure the base class's — or nobody's —
    loop. The marker is the loop header *text*, so a rename of the bound
    variable makes the measurement fail to find its subject rather than moving
    it onto a different loop, which is the honest outcome.
    """

    name: str
    relpath: str
    qualifier: str
    function: str
    marker: str


@dataclass(frozen=True)
class LoopComplexity:
    """One measured loop body."""

    name: str
    relpath: str
    qualifier: str
    function: str
    marker: str
    body_start_line: int
    body_end_line: int
    lines: int
    statements: int
    branches: int
    conditionals: int
    policy_branches: int
    #: Branches inside closures DEFINED in the body. Excluded from
    #: ``branches``/``statements``/``conditionals`` and reported here so the
    #: exclusion is visible rather than a silently smaller number.
    nested_branches: int = 0
    policy_names: tuple[str, ...] = field(default=())
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relpath": self.relpath,
            "qualifier": self.qualifier,
            "function": self.function,
            "marker": self.marker,
            "body_start_line": self.body_start_line,
            "body_end_line": self.body_end_line,
            "lines": self.lines,
            "statements": self.statements,
            "branches": self.branches,
            "conditionals": self.conditionals,
            "policy_branches": self.policy_branches,
            "nested_branches": self.nested_branches,
            "policy_names": list(self.policy_names),
            "error": self.error,
        }


#: The two agent turn loops in the tree. ``harness/agent_loop.py`` is absent on
#: purpose: it contains no turn loop at all (it is dispatch/adapter code), and
#: naming it here would invite a pin that "measures" an empty span.
LOOPS: tuple[LoopRef, ...] = (
    LoopRef(
        name="agent_step",
        relpath="harness/agent_loop_step.py",
        qualifier="",
        function="step",
        marker="while turn < cfg.max_turns:",
    ),
    LoopRef(
        name="daily_kernel",
        relpath="harness/agent_kernel/strategy.py",
        qualifier="DailyCodingStrategy",
        function="run",
        marker="for turn in range(1, max_turns + 1):",
    ),
)

#: Closed vocabulary of the turn-level policies this tree has accumulated. A
#: branch counts as a *policy* branch when its own source text names one of
#: these. The list is data so a reader can argue with it, and it is asserted to
#: cover every shipped middleware by a pin — a policy composed into the pipeline
#: whose trigger words are absent from this vocabulary would stop being counted,
#: and the whole ratchet would silently stop measuring.
POLICY_VOCABULARY: frozenset[str] = frozenset(
    {
        # turn ceiling / count
        "agent_max_turns",
        "max_turns",
        "turn_caps",
        "approach_observation",
        "max_tool_recoveries",
        "max_model_failures",
        "_validation_recoveries",
        "recovery_count",
        "model_failures",
        # money
        "budget_cap_usd",
        "total_cost_usd",
        "spent_usd",
        "authorize_call",
        "reserved_usd",
        "BudgetRefused",
        # clock / cancellation
        "max_wallclock_s",
        "elapsed_s",
        "deadline",
        "_is_cancelled",
        "cancelled",
        "CancellationStatus",
        # context
        "_prepared_request",
        "compact",
        "context_utilization",
        "ContextBudget",
        "budget_from_config",
        # doom loop / repeat guard
        "repeats",
        "max_repeat_tool_calls",
        "loop_guard",
        "_doom_loop_decision",
        "note_repeat",
        # approval / policy
        "require",
        "APPROVAL_TOOLS",
        "approved",
        "approval",
        "policy_refused",
        "command_rejected",
        "permission_denied",
        "environment_unavailable",
        # reflection / recovery budget
        "reflect",
        "reflections",
        "may_continue",
        "exhausted",
        "tool_recovery",
        "model_recovery",
        "classify_model_failure",
        # read-only mode
        "_read_only",
        "loop_guard_read_only",
        "read_only",
        # quota / backoff
        "QuotaExhausted",
        "quota",
        "backoff",
        "retryable",
        "provider_fault",
        # steering
        "steering",
        "_steering_action",
    }
)

#: Measured BEFORE the middleware refactor, by this module, on this tree.
#: Recorded once, asserted forever (or until somebody tightens it deliberately).
#: Each value is ``(lines, statements, branches, conditionals, policy_branches)``.
#:
#: These were taken with this same instrument rather than by hand, so the two
#: columns of the report come from one measurement method. The numbers were
#: first written down as estimates and were **wrong** on three axes; the
#: instrument's output replaced them. That is the reason the module exists.
#:
#: The load-bearing row is ``policy_branches``: 31 in-loop turn-level policy
#: decisions across the two loops. R1's rule says to graduate to a middleware
#: pipeline "once you need three or more independent turn-level policies", and
#: that is the count the rule is measured against.
BASELINE: dict[str, dict[str, int]] = {
    "agent_step": {
        "lines": 336,
        "statements": 113,
        "branches": 34,
        "conditionals": 31,
        "policy_branches": 18,
    },
    "daily_kernel": {
        "lines": 192,
        "statements": 67,
        "branches": 15,
        "conditionals": 14,
        "policy_branches": 13,
    },
}

#: R1's declared graduation threshold, quoted so the ratchet above can be read
#: against it. Three is the corpus's rule; the measured count is an order of
#: magnitude past it.
R1_GRADUATION_THRESHOLD = 3


_BRANCH_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.IfExp,
    ast.Try,
    ast.For,
    ast.AsyncFor,
    ast.While,
)
_CONDITIONAL_NODES: tuple[type[ast.AST], ...] = (ast.If, ast.IfExp)
_MATCH_NODE = getattr(ast, "Match", None)


def _source_of(ref: LoopRef) -> tuple[str, str]:
    """Return ``(source, relative_path)`` for a loop ref.

    Reads ``utf-8-sig``: a UTF-8 BOM makes ``ast.parse`` raise on line 1, and a
    reader that skipped unparseable files would report the BOM'd file as
    *measuring zero* — which is the exact vacuity this repository has been
    bitten by more than once.
    """
    path = REPO_ROOT / ref.relpath
    return path.read_text(encoding="utf-8-sig"), ref.relpath


def _find_loop_node(tree: ast.Module, ref: LoopRef, source: str) -> ast.AST | None:
    """Locate the loop node named by ``ref``, or ``None``.

    Resolution order is class-then-function-then-header-text, and it is
    deliberately that order: ``ast.walk`` is breadth-first over the whole
    module, so a function-name match alone picks whichever ``run`` it meets
    first. In ``strategy.py`` that is the abstract base class's ``run``, which
    contains no loop at all — a measurement that reported "0 branches" for the
    base class would be a plausible-looking lie.

    A module-level function is reached with an empty ``qualifier``; a class
    method requires the class name to match.
    """
    owners: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != ref.function:
                continue
            if ref.qualifier:
                enclosing = _enclosing_class(tree, node)
                if enclosing != ref.qualifier:
                    continue
            owners.append(node)
    if not owners:
        return None
    for target in owners:
        lines = source.splitlines()
        for node in ast.walk(target):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            index = node.lineno - 1
            if 0 <= index < len(lines) and ref.marker in lines[index]:
                return node
    return None


def _enclosing_class(tree: ast.Module, target: ast.AST) -> str:
    """The name of the innermost ``class`` containing ``target``, or ``""``."""
    best: str = ""
    best_depth = -1
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in ast.walk(node):
            if child is target:
                depth = sum(1 for _ in ast.walk(node) if isinstance(_, ast.ClassDef))
                if depth > best_depth:
                    best, best_depth = node.name, depth
                break
    return best


def _policy_terms_in(node: ast.AST) -> set[str]:
    """Policy-vocabulary terms appearing anywhere in ``node``'s own subtree."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
    return found & POLICY_VOCABULARY


def measure_loop(ref: LoopRef, *, include_nested: bool = False) -> LoopComplexity:
    """Measure one declared loop body.

    ``include_nested=True`` reproduces the FIRST version's arithmetic, which
    counted branches inside closures defined in the body as the loop's own.
    That over-counts -- a closure is a function with its own complexity -- and
    the default (``False``) is the corrected measure. The legacy mode exists so
    the stored :data:`BASELINE` stays comparable: both sides of the printed
    delta come from the same instrument, and ``nested_branches`` reports what
    the two differ by. Measured on the daily kernel's turn loop: 17 branches
    legacy, 9 corrected, all 8 in two small local helpers the loop merely calls.

    Never raises: an unreadable or unparseable source, or a moved loop header,
    produces a ``LoopComplexity`` with ``error`` set and every count at zero.
    A *zero* is therefore never a passing measurement here — callers must check
    ``error`` (and ``measure_all()`` does), because "the loop measured zero
    branches" and "the loop could not be found" are the same number and mean
    opposite things.
    """
    try:
        source, _rel = _source_of(ref)
        tree = ast.parse(source, filename=ref.relpath)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return LoopComplexity(
            name=ref.name,
            relpath=ref.relpath,
            qualifier=ref.qualifier,
            function=ref.function,
            marker=ref.marker,
            body_start_line=0,
            body_end_line=0,
            lines=0,
            statements=0,
            branches=0,
            conditionals=0,
            policy_branches=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    node = _find_loop_node(tree, ref, source)
    if node is None:
        return LoopComplexity(
            name=ref.name,
            relpath=ref.relpath,
            qualifier=ref.qualifier,
            function=ref.function,
            marker=ref.marker,
            body_start_line=0,
            body_end_line=0,
            lines=0,
            statements=0,
            branches=0,
            conditionals=0,
            policy_branches=0,
            error=f"the loop header {ref.marker!r} was not found in {ref.function}()",
        )

    body = list(node.body)
    if not body:
        return LoopComplexity(
            name=ref.name,
            relpath=ref.relpath,
            qualifier=ref.qualifier,
            function=ref.function,
            marker=ref.marker,
            body_start_line=node.lineno,
            body_end_line=node.lineno,
            lines=0,
            statements=0,
            branches=0,
            conditionals=0,
            policy_branches=0,
            error="the loop header matched a loop with an empty body",
        )
    start = body[0].lineno
    # The span is the LAST BODY STATEMENT's end, not the loop node's own
    # `end_lineno`. A `while ... else:` clause is outside the body, and counting
    # it would make `lines` and `statements` disagree about what "the body" is
    # — which is the kind of mismatch that lets a reordering flatter the report.
    end = max(int(getattr(stmt, "end_lineno", None) or stmt.lineno) for stmt in body)

    branches = 0
    conditionals = 0
    policy_branches = 0
    names: set[str] = set()
    statements = 0
    #: Branches inside closures defined in the body. Excluded from the axes above
    #: and reported here, so the exclusion is visible rather than a silently
    #: smaller number.
    nested_branches = 0
    for statement in body:
        if include_nested:
            # LEGACY semantics, kept so the stored BASELINE stays comparable.
            # This over-counts: a closure's branches are that function's
            # complexity, not the loop's. Both figures are printed, and the
            # before/after delta is taken on this one because that is the
            # instrument the baseline was measured with.
            for sub in ast.walk(statement):
                if _is_branch(sub):
                    branches += 1
                if isinstance(sub, _CONDITIONAL_NODES):
                    conditionals += 1
                if isinstance(sub, _BRANCH_NODES):
                    terms = _policy_terms_in(sub)
                    if terms:
                        policy_branches += 1
                        names |= terms
                if isinstance(sub, ast.stmt):
                    statements += 1
            continue
        # Start from the STATEMENT itself, not its children: `ast.walk(stmt)`
        # includes `stmt`, and a top-level `if` in the loop body is one branch.
        # Seeding the stack with the children drops exactly those, which is how
        # the first corrected version silently under-counted by 13 on
        # `agent_step` while reporting a confident smaller number.
        stack = [statement]
        while stack:
            sub = stack.pop()
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                nested_branches += _count_branches(sub)
                continue
            if _is_branch(sub):
                branches += 1
            if isinstance(sub, _CONDITIONAL_NODES):
                conditionals += 1
            if isinstance(sub, _BRANCH_NODES):
                terms = _policy_terms_in(sub)
                if terms:
                    policy_branches += 1
                    names |= terms
            if isinstance(sub, ast.stmt):
                statements += 1
            stack.extend(ast.iter_child_nodes(sub))

    return LoopComplexity(
        name=ref.name,
        relpath=ref.relpath,
        qualifier=ref.qualifier,
        function=ref.function,
        marker=ref.marker,
        body_start_line=start,
        body_end_line=end,
        lines=end - start + 1,
        statements=statements,
        branches=branches,
        conditionals=conditionals,
        policy_branches=policy_branches,
        nested_branches=nested_branches,
        policy_names=tuple(sorted(names)),
    )


def _is_branch(node: ast.AST) -> bool:
    """Whether a node counts as a branch for the ratchet.

    The ``match`` case is written with explicit parentheses rather than relying
    on ``and``-over-``or`` precedence: ``_MATCH_NODE is not None and isinstance(x,
    _MATCH_NODE) or isinstance(x, _BRANCH_NODES)`` reads correctly but is one
    edit away from not being what it looks like, and this is the predicate the
    headline number is built from.
    """
    if isinstance(node, _BRANCH_NODES):
        return True
    return _MATCH_NODE is not None and isinstance(node, _MATCH_NODE)


def _count_branches(node: ast.AST) -> int:
    """Branches inside a closure, so the exclusion is measured not assumed."""
    total = 0
    stack = list(ast.iter_child_nodes(node))
    while stack:
        sub = stack.pop()
        if _is_branch(sub):
            total += 1
        stack.extend(ast.iter_child_nodes(sub))
    return total


def measure_all(loops: tuple[LoopRef, ...] = LOOPS) -> dict[str, LoopComplexity]:
    """Measure every declared loop, keyed by name."""
    return {ref.name: measure_loop(ref) for ref in loops}


def regressions(
    measured: dict[str, LoopComplexity] | None = None,
    baseline: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Rows where a loop measures ABOVE its baseline on any axis.

    A measurement error is reported as a regression rather than skipped: "I
    could not measure the loop" must never be indistinguishable from "the loop
    is fine", which is the same shape as the honesty rules' "an absent value is
    not a zero".
    """
    measured = measured if measured is not None else measure_all()
    baseline = baseline if baseline is not None else BASELINE
    rows: list[dict[str, Any]] = []
    for name, base in baseline.items():
        got = measured.get(name)
        if got is None:
            rows.append({"name": name, "axis": "presence", "baseline": "declared", "measured": "absent"})
            continue
        if got.error:
            rows.append({"name": name, "axis": "measurable", "baseline": "yes", "measured": got.error})
            continue
        for axis in ("lines", "statements", "branches", "conditionals", "policy_branches"):
            value = getattr(got, axis)
            if value > int(base[axis]):
                rows.append({"name": name, "axis": axis, "baseline": int(base[axis]), "measured": value})
    return rows


def complexity_table(
    measured: dict[str, LoopComplexity] | None = None,
    baseline: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Before/after rows for every declared loop and axis.

    The "after" column is ``None`` when a loop could not be measured, never
    ``0``. A zero reduction would read as "the refactor deleted the loop".
    """
    measured = measured if measured is not None else measure_all()
    baseline = baseline if baseline is not None else BASELINE
    rows: list[dict[str, Any]] = []
    for ref in LOOPS:
        got = measured.get(ref.name)
        base = baseline.get(ref.name, {})
        for axis in ("lines", "statements", "branches", "conditionals", "policy_branches"):
            after: int | None = None
            if got is not None and not got.error:
                after = int(getattr(got, axis))
            before = base.get(axis)
            reduction: int | None = None
            if before is not None and after is not None:
                reduction = int(before) - after
            rows.append(
                {
                    "loop": ref.name,
                    "file": ref.relpath,
                    "axis": axis,
                    "before": before,
                    "after": after,
                    "reduction": reduction,
                    "error": got.error if got is not None else "not measured",
                }
            )
    return rows


def report(
    measured: dict[str, LoopComplexity] | None = None,
    baseline: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """The whole receipt: per-loop measurements, the before/after table, and the
    regression verdict.

    The before/after comparison is taken on the LEGACY instrument
    (:func:`measure_loop` with ``include_nested=True``), because that is the
    instrument :data:`BASELINE` was measured with and a delta across two
    different instruments is not a delta. The corrected per-loop figures are
    published alongside under ``corrected``, with the nested-closure count that
    explains the difference, so both numbers are available and neither is
    presented as the other.
    """
    measured = measured if measured is not None else measure_all()
    comparable = {ref.name: measure_loop(ref, include_nested=True) for ref in LOOPS}
    rows = complexity_table(comparable, baseline)
    rows_regressed = regressions(comparable, baseline)
    return {
        "loops": {name: got.to_dict() for name, got in measured.items()},
        "baseline_instrument": "include_nested=True (branches inside closures counted)",
        "corrected": {
            name: {
                "branches": got.branches,
                "conditionals": got.conditionals,
                "statements": got.statements,
                "policy_branches": got.policy_branches,
                "nested_branches": got.nested_branches,
            }
            for name, got in measured.items()
        },
        "total_policy_branches_after_corrected": sum(
            got.policy_branches for got in measured.values() if not got.error
        ),
        "table": rows,
        "policy_vocabulary_size": len(POLICY_VOCABULARY),
        "baseline": baseline if baseline is not None else BASELINE,
        "regressions": rows_regressed,
        "total_policy_branches_before": sum(
            int(v["policy_branches"]) for v in (baseline or BASELINE).values()
        ),
        "total_policy_branches_after": sum(
            got.policy_branches for got in measured.values() if not got.error
        ),
        "ok": not rows_regressed,
    }


def render_report(payload: dict[str, Any]) -> str:
    """Render the receipt as plain text for a terminal."""
    lines: list[str] = []
    lines.append("loop-body complexity, measured from the AST")
    lines.append("")
    header = f"{'loop':<14} {'axis':<17} {'before':>7} {'after':>7} {'delta':>7}"
    lines.append(header)
    lines.append("-" * len(header))
    current = ""
    for row in payload["table"]:
        if row["loop"] != current:
            current = row["loop"]
            lines.append(f"{row['loop']}  ({row['file']})")
        after = "-" if row["after"] is None else str(row["after"])
        before = "?" if row["before"] is None else str(row["before"])
        delta = "-" if row["reduction"] is None else f"{row['reduction']:+d}"
        lines.append(f"{'':<14} {row['axis']:<17} {before:>7} {after:>7} {delta:>7}")
        if row["error"]:
            lines.append(f"{'':<14} {'  !! ' + row['error']}")
    lines.append("")
    lines.append(
        "policy branches total: "
        f"{payload['total_policy_branches_before']} -> {payload['total_policy_branches_after']}"
    )
    lines.append(
        "  (table above: legacy instrument, branches inside closures counted -- "
        "the instrument the baseline was measured with)"
    )
    lines.append(
        "corrected (closures excluded): "
        f"policy branches {payload['total_policy_branches_after_corrected']}, "
        "branches "
        + ", ".join(
            f"{name} {got['branches']} (+{got['nested_branches']} nested)"
            for name, got in payload["corrected"].items()
        )
    )
    lines.append(f"policy vocabulary size: {payload['policy_vocabulary_size']}")
    if payload["regressions"]:
        lines.append("")
        lines.append("REGRESSIONS (a loop measured above its baseline):")
        for row in payload["regressions"]:
            lines.append(f"  {row['name']}.{row['axis']}: {row['measured']} > {row['baseline']}")
    else:
        lines.append("")
        lines.append("no loop is above its baseline on any axis")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the complexity receipt. Exit 2 if any loop regressed."""
    parser = argparse.ArgumentParser(prog="python -m runtime.loop_complexity")
    parser.add_argument("--json", action="store_true", help="machine-readable receipt")
    args = parser.parse_args(argv)

    payload = report()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_report(payload))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(main())
