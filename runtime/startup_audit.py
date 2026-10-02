"""T3.W1.4 — classify every module-level expensive operation in runtime/.

Three detectors, because "expensive at import time" has four shapes and the
fourth is the one that actually cost 318 ms on this tree:

* **CALLS** — a module-level function call that is not obviously constant
  folding. These are the ones that actually run at import.
* **COMPREHENSIONS** — a module-level comprehension, which materialises a
  container from a loop and is invisible to a call-only scan.
* **LITERALS** — a large literal, i.e. a table the import has to build.
* **CROSS-MODULE IMPORTS** — a module-level ``from <first-party> import ...``.
  Added after measuring: ``runtime/roles.py:19`` does
  ``from harness.agent_kernel import ...`` and that single line costs
  **318 ms**, while the dict comprehension it feeds costs **0.029 ms**. A
  scanner with no import detector would have reported the 0.029 ms line and
  missed the 318 ms one entirely — which is the most expensive possible way
  to be wrong about an audit.

Each hit is classified and a verdict is required: `preloaded`, `deferred`,
`constant`, or `accepted`. A hit with no verdict is the failure, because an
unclassified expensive operation is the defect class this round exists to
audit, and a scanner that reports a count instead of a verdict has not
answered the question.

Not a timing tool. Timing is measured separately (see runtime/latency.py's
docstring table); this is a structural scan, because a scan that only runs on
a fast machine cannot see an operation that is fast on a fast machine.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RUNTIME_DIR = Path(__file__).resolve().parent

#: Verdict vocabulary. A classified hit MUST name one.
VERDICTS = ("preloaded", "deferred", "constant", "accepted")

#: Calls that are known-cheap and would otherwise drown the report. Every entry
#: is here because it is a pure stdlib constant or a trivial constructor; a
#: call that is NOT here is reported for a human to rule on.
CHEAP_CALLS = frozenset(
    {
        "frozenset",
        "set",
        "dict",
        "list",
        "tuple",
        "bytes",
        "bytearray",
        "int",
        "float",
        "bool",
        "str",
        "complex",
        "range",
        "object",
        "sorted",
        "len",
        "min",
        "max",
        "sum",
        "abs",
        "round",
        "repr",
        "hash",
        "isinstance",
        "getattr",
        "hasattr",
        "callable",
        "re.compile",
        "re.escape",
        "re.Pattern",
        "Path",
        "TypeVar",
        "Optional",
        "Dict",
        "List",
        "Tuple",
        "Set",
        "FrozenSet",
        "Any",
        "Callable",
        "Mapping",
        "Iterable",
        "Sequence",
        "Union",
        "NamedTuple",
        "field",
        "dataclass",
        "defaultdict",
        "Counter",
        "OrderedDict",
        "deque",
        "Enum",
        "IntEnum",
        "StrEnum",
        "staticmethod",
        "classmethod",
        "property",
        "lru_cache",
        "wraps",
    }
)

#: Calls that are expensive BY CONSTRUCTION on a latency path. A match here is
#: a finding, not a candidate.
EXPENSIVE_CALL_NAMES = (
    "sqlite3.connect",
    "connect",
    "urlopen",
    "request",
    "get",
    "post",
    "run",
    "Popen",
    "check_output",
    "listdir",
    "walk",
    "glob",
    "rglob",
    "read_bytes",
    "read_text",
    "load",
    "loads",
    "dumps",
)

#: First-party top-level packages. A module-level `from <one of these> import`
#: inside `runtime/` drags that whole package's import graph in, which is the
#: shape that cost 318 ms at `runtime/roles.py:19`.
FIRST_PARTY_PACKAGES = frozenset(
    {"cli", "execution", "harness", "memory", "shared", "extensions", "mcp_server"}
)

#: Verdict per (file, kind) for the hits measured in this tree. Anything NOT
#: listed here is reported as `UNCLASSIFIED`, which the caller must rule on.
#:
#: Every reason below is a MEASUREMENT on this host, not an opinion. The
#: numbers are recorded so a future reader can tell a verdict that has been
#: re-measured from one that was guessed.
VERDICTS_BY_SITE: Dict[Tuple[str, str], Tuple[str, str]] = {
    # --- constant: bounded, pure, and measured in the sub-millisecond range
    ("model_capabilities.py", "comprehension"): (
        "constant",
        "_BUILTIN_EFFORT_KNOBS: 3 entries, measured 0.0 ms. A capability "
        "table the router reads on every call; deferring it would move a "
        "measured non-cost onto the call path for nothing.",
    ),
    ("model_capabilities.py", "literal"): (
        "constant",
        "__all__: 71 interned strings, measured 0.0 ms. It is also the "
        "reachability contract the module tests assert against.",
    ),
    ("privacy_policy.py", "comprehension"): (
        "constant",
        "_CLASSES: 6 entries, measured 0.0 ms. A closed policy vocabulary "
        "that must be complete at import or a strict policy could refuse "
        "nothing.",
    ),
    ("multirepo_tasks.py", "call"): (
        "constant",
        "CACHE_ROOT: one os.environ.get plus a Path join, measured 0.0 ms. "
        "The module's 130 ms total import is dominated by the shared runtime "
        "import graph, not by this line.",
    ),
    # --- deferred: a real cost, on a real path, recorded rather than done
    ("roles.py", "comprehension"): (
        "deferred",
        "_TOOL_SPECS: the dict build is measured 0.029 ms over 45 specs. "
        "NOT the cost on this line -- see the cross_module_import verdict.",
    ),
    ("roles.py", "cross_module_import"): (
        "deferred",
        "MEASURED 318 ms for `from harness.agent_kernel import ...`, against "
        "0.029 ms for the comprehension it feeds. This is the largest "
        "module-level cost found anywhere in runtime/ and it is on the WORKER "
        "START path, not the per-call path (nothing under model_router / "
        "provider_gateway imports runtime.roles). Not fixed here: "
        "_TOOL_SPECS is module-level public state read by other modules, so "
        "making it lazy is a shape change to another owner's file. Recorded "
        "as the single highest-value follow-up in this audit.",
    ),
    ("orchestration.py", "cross_module_import"): (
        "deferred",
        "Same 318 ms harness.agent_kernel import, reached through "
        "runtime.orchestration. Orchestration is a batch path (a DAG of "
        "children), so this is paid once per orchestration process rather "
        "than per call.",
    ),
    ("orchestration_worker.py", "cross_module_import"): (
        "deferred",
        "Same import, in the child worker. Paid once per child process.",
    ),
    ("worker.py", "cross_module_import"): (
        "deferred",
        "Same import, in the task worker. Paid once per task process; a "
        "50-way fan-out pays it 50 times in PARALLEL, which is why this is "
        "worth someone's time even though no single run feels it.",
    ),
    ("subagents.py", "cross_module_import"): (
        "deferred",
        "Same import, via harness.agent_kernel.subagents. Paid once per "
        "process that spawns or describes agents.",
    ),
    ("ablation.py", "cross_module_import"): (
        "deferred",
        "MEASURED 361 ms for `from harness.agent_kernel import ...`, against "
        "0.029 ms for the comprehension it feeds. An offline analysis "
        "driver; cost is irrelevant to a run.",
    ),
}

#: Measured cold-import cost of each first-party package, in milliseconds, on
#: this host (`win32`, CPython 3.10, median of 3 cold subprocesses). Recorded
#: here so a verdict about a cross-module import can cite a number instead of
#: an intuition, and so a regression in one of these is visible as a change to
#: a literal rather than as a mysteriously slower CLI.
FIRST_PARTY_IMPORT_MS: Dict[str, float] = {
    "shared": 3.32,
    "shared.types": 53.82,
    "shared.egress": 68.73,
    "shared.security": 102.35,
    "shared.tracing": 115.31,
    "shared.approval": 118.42,
    "harness.agent_kernel": 360.94,
    "harness.agent_kernel.subagents": 358.48,
}

#: Anything at or under this is a `constant` for latency purposes: below the
#: noise floor of a CLI start, and below the jitter a user could perceive.
FIRST_PARTY_IMPORT_MS_FLOOR = 150.0


def _verdict_for_cross_module(module: str, label: str) -> Tuple[str, str]:
    """Classify one cross-module import from its MEASURED cost.

    The floor is 150 ms, chosen against the two things it has to be compared
    to: the 0.78 s `neo --version` this project already treats as healthy,
    and the ~6-22 s litellm import this round exists to remove. A 100 ms
    import is 1/8th of one and 1/60th of the other — it is not what makes
    the tool feel slow, and deferring it would add indirection to a
    correctness boundary (a redaction authority, a type contract) for a
    latency win nobody can feel.
    """
    ms = FIRST_PARTY_IMPORT_MS.get(label)
    if ms is None:
        return (
            "UNCLASSIFIED",
            f"no measured import cost recorded for {label!r}; measure it before"
            " ruling on it",
        )
    if ms <= FIRST_PARTY_IMPORT_MS_FLOOR:
        return (
            "constant",
            f"MEASURED {ms:.1f} ms to import {label} (median of 3 cold"
            f" subprocesses), at or under the {FIRST_PARTY_IMPORT_MS_FLOOR:.0f}"
            " ms floor. It is also a correctness boundary — a redaction"
            " authority, a serialized type contract — and deferring one to"
            " save a latency nobody can feel trades a real guarantee for"
            " noise.",
        )
    return (
        "deferred",
        f"MEASURED {ms:.1f} ms to import {label}, ABOVE the"
        f" {FIRST_PARTY_IMPORT_MS_FLOOR:.0f} ms floor. This is the shape worth"
        " someone's time; the reasoning is recorded per-site above where the"
        " path it is on is known.",
    )


@dataclass(frozen=True)
class ExpensiveOp:
    """One module-level operation that costs something at import time."""

    module: str
    line: int
    kind: str
    label: str
    verdict: str = "UNCLASSIFIED"
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "module": self.module,
            "line": self.line,
            "kind": self.kind,
            "label": self.label,
            "verdict": self.verdict,
            "reason": self.reason,
        }


def _is_main_guard(node: ast.AST) -> bool:
    """Whether a module-level ``if`` is the ``__main__`` guard."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    comparator = test.comparators[0]
    return isinstance(comparator, ast.Constant) and comparator.value == "__main__"


def _call_name(node: ast.AST) -> str:
    """Best-effort dotted name for a call target. ``''`` when unknown."""
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _assigned_names(target: ast.AST) -> List[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out: List[str] = []
        for element in target.elts:
            out.extend(_assigned_names(element))
        return out
    return []


def _literal_size(node: ast.AST) -> int:
    """A cheap proxy for how big a literal is. Not a memory measurement."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return len(node.elts)
    if isinstance(node, ast.Dict):
        return len(node.keys)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value)
    return 0


def scan_module(path: Path) -> List[ExpensiveOp]:
    """Return every module-level expensive operation in one file.

    Only module-level statements are considered. A call inside a function is
    not paid at import, which is exactly the distinction the litellm case
    turned on: the import was already lazy, and the cost was at first call.
    """
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A file that does not parse is a DIFFERENT defect, and skipping it
        # silently would turn "I could not read this file" into "this file is
        # clean" — the same hole a T1 round documented for its own scanner.
        return [
            ExpensiveOp(
                module=path.name,
                line=0,
                kind="unparseable",
                label="<file does not parse>",
                verdict="UNCLASSIFIED",
                reason="ast.parse raised SyntaxError; the scan did not skip it silently",
            )
        ]
    found: List[ExpensiveOp] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # A `if __name__ == "__main__":` block is module-level SYNTACTICALLY
        # and can never execute at import. Reporting it would be a false
        # positive, and a scanner whose output is 50% noise is a scanner
        # nobody reads.
        if _is_main_guard(node):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = _call_name(sub.func)
                if not name or name.split(".")[0] in CHEAP_CALLS:
                    continue
                if name not in EXPENSIVE_CALL_NAMES and not any(
                    name.endswith("." + suffix) for suffix in EXPENSIVE_CALL_NAMES
                ):
                    continue
                found.append(
                    ExpensiveOp(
                        module=path.name,
                        line=sub.lineno,
                        kind="call",
                        label=name or "<unknown call>",
                    )
                )
            elif isinstance(
                sub, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                # A module-level generator expression is LAZY: nothing runs
                # until it is consumed. A comprehension is not, and at module
                # level it usually is not consumed at all.
                if isinstance(sub, ast.GeneratorExp):
                    continue
                found.append(
                    ExpensiveOp(
                        module=path.name,
                        line=sub.lineno,
                        kind="comprehension",
                        label=type(sub).__name__,
                    )
                )
    for node in tree.body:
        if isinstance(node, ast.Assign) and _literal_size(node.value) >= 64:
            for target in _assigned_names(node.targets[0]):
                found.append(
                    ExpensiveOp(
                        module=path.name,
                        line=node.lineno,
                        kind="literal",
                        label=f"{target} = <{_literal_size(node.value)} items>",
                    )
                )
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FIRST_PARTY_PACKAGES and root != path.stem:
                found.append(
                    ExpensiveOp(
                        module=path.name,
                        line=node.lineno,
                        kind="cross_module_import",
                        label=node.module or "",
                    )
                )
    return found


def scan_runtime(root: Optional[Path] = None) -> List[ExpensiveOp]:
    """Scan every ``runtime/*.py``. Public: this is the W1.4 evidence."""
    base = Path(root) if root is not None else RUNTIME_DIR
    out: List[ExpensiveOp] = []
    for path in sorted(base.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        out.extend(scan_module(path))
    return out


def classify(found: Optional[List[ExpensiveOp]] = None) -> List[ExpensiveOp]:
    """Attach a verdict to every hit. Unclassified hits are returned as-is."""
    ops = scan_runtime() if found is None else found
    out: List[ExpensiveOp] = []
    for op in ops:
        recorded = VERDICTS_BY_SITE.get((op.module, op.kind))
        if recorded is not None:
            verdict, reason = recorded
        elif op.kind == "cross_module_import":
            # No hand-written verdict: the cost table decides, so a new
            # cross-module import is classified the moment it is measured
            # rather than becoming an open audit item.
            verdict, reason = _verdict_for_cross_module(op.module, op.label)
        else:
            verdict, reason = "UNCLASSIFIED", ""
        out.append(
            ExpensiveOp(
                module=op.module,
                line=op.line,
                kind=op.kind,
                label=op.label,
                verdict=verdict,
                reason=reason or "no verdict recorded; a human must rule on this",
            )
        )
    return out


def enumeration_table() -> List[Dict[str, object]]:
    """The publishable table: every hit with its verdict."""
    return [op.to_dict() for op in classify()]


def unclassified() -> List[ExpensiveOp]:
    """Hits with no verdict. A non-empty result is an audit failure."""
    return [op for op in classify() if op.verdict == "UNCLASSIFIED"]


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import json

    print(json.dumps(enumeration_table(), indent=2, sort_keys=True))
