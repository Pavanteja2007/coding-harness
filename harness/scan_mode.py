"""Scan mode — proactive, read-only codebase health analysis.

Without a specific bug report, analyze a repo using the EXISTING
structural/retrieval infrastructure (Terminal 4's memory.code_graph via
harness.deps, the same graph retrieval and coordination consume) plus
stdlib AST passes, and surface the opportunities genuinely worth a
developer's attention:

- ``coverage_gap``  modules / load-bearing functions no test exercises
- ``smell``         latent-bug code smells (mutable defaults, bare
                    except, silently swallowed exceptions)
- ``dependency``    outdated pins (opt-in PyPI check) and cross-manifest
                    pin conflicts

Design (mirrors qa_mode/research_mode — read-only BY CONSTRUCTION):

- No shell, no sandbox, no editor, no pristine/work copies, and NO
  model calls: findings come from the graph + AST + manifests, and
  the ranking/rationales are deterministic from the finding's own
  evidence — the same discipline as execution.rationale ("the trace
  already contains every fact needed"). A scan is instant, offline,
  and reproducible. The only remote call is the opt-in PyPI JSON
  check (``scan_remote_deps`` / ``--remote``), gated OFF by default
  exactly like ``docs_lookup_allow_remote``.
- **Noise budget** (Task B): findings are scored (severity + real
  structural signals like fan-in) and the REPORT shows only the top
  ``scan_max_findings`` (default 8); the rest stay in scan.json,
  resurfaced via ``--max-findings``. A scan that dumps 50 low-value
  findings is worse than one with 5 real ones — the budget is the
  product decision, not an afterthought.
- **Rationale per finding** (Task B): 2-4 grounded sentences in the
  rationale-log style already built for fixes — what was found, why
  it deserves attention, what the first step is. Deterministic, never
  invented.
- **Task C handoff**: every finding carries a fix contract
  (``fix_kind`` / ``fix_issue_text`` / ``fix_target_test``) so
  ``vex fix --finding <scan_id>#<n>`` (or ``vex scan --fix <n>``)
  turns "Vex noticed this" into a REAL task through the existing,
  verifier-gated entries — the fix loop for coverage/smell findings
  (the suggested test file is the target: it must not exist on the
  pristine tree, so the baseline gate is honest, and the loop's
  "don't modify tests" rule is explicitly authorized by the issue
  text) and build mode for dependency bumps (a version-floor
  acceptance test genuinely fails on the old pin).

Config keys (harness/config.py): scan_max_findings (8),
scan_remote_deps (False), scan_pypi_timeout_s (10),
scan_smells_per_kind (3), scan_func_gap_max (3).

Detectors are Python-first (AST + the graph's Python indexing); a repo
with no .py sources reports that honestly rather than guessing. JS/TS
graph support exists upstream but the smell AST pass does not —
documented limitation, not a silent skip.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
import urllib.request
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from harness.config import get_config
from harness.retrieval import _module_id_for
from harness.trace import TraceLogger

__all__ = ["finding_task_params", "resolve_finding", "run_scan"]

_SEVERITY_SCORE = {"high": 30.0, "medium": 20.0, "low": 10.0}
_KIND_WEIGHT = {"coverage_gap": 1.2, "smell": 1.0, "dependency": 1.1}

_SKIP_DIRS = {
    "__pycache__",
    "node_modules",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "site-packages",
    "venv",
    # Harness/vcs output roots — never scan targets (cloned fixture
    # repos under logs/ are third-party code, not the project's).
    "logs",
    ".git",
    ".harness",
    ".vex",
    # Tool/audit output (probe scripts are run evidence, not product code).
    "probe_logs",
    ".opencode",
    ".qwen",
    ".playwright-mcp",
    ".shots",
    "graphify-out",
    "Temp",
}


def _iter_py_files(repo_path: str) -> List[str]:
    """Repo-relative posix .py paths, junk/vendored dirs excluded.

    Assumes repo_path is a readable directory. Dot-directories and the
    build-artifact set above are skipped (harness artifacts like
    ``.harness/`` and tool caches are never scan targets). Never
    raises — an unreadable subtree is skipped, not fatal.
    """
    out: List[str] = []
    root = Path(repo_path)
    for dirpath, dirnames, filenames in (
        root.walk() if hasattr(root, "walk") else _walk(root)
    ):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
        ]
        for name in filenames:
            if not name.endswith(".py") or name.startswith("."):
                continue
            out.append((Path(dirpath) / name).relative_to(root).as_posix())
    return out


def _walk(root: Path):
    """os.walk-compatible generator for _iter_py_files on old Pythons."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        yield dirpath, dirnames, filenames


def _is_test_file(rel: str) -> bool:
    """True if a repo-relative .py path is test infrastructure.

    Assumes rel is a normalized posix path. A file is test infra when
    it sits under a tests-like directory (tests/, test/, __tests__/,
    spec/) or its basename matches test_*.py / *_test.py / conftest.py.
    """
    parts = rel.split("/")
    base = parts[-1]
    if any(p in ("tests", "test", "__tests__", "spec", "testing") for p in parts[:-1]):
        return True
    return (
        base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"
    )


def _excluded_path(rel: str) -> bool:
    """True for paths that are scan noise (artifact/tool dirs, non-Python).

    The graph indexes whatever it finds under the repo root; scan
    findings must only ever point at the project's own Python source.
    """
    if not rel or not rel.endswith(".py"):
        return True
    parts = rel.split("/")
    return any(p in _SKIP_DIRS for p in parts[:-1])


def _is_scan_infra(rel: str) -> bool:
    """Files that are harness/scan infrastructure, never findings."""
    base = rel.split("/")[-1]
    return base in ("conftest.py", "setup.py")


def _load_graph(repo_path: str, index_root: Optional[Path]) -> Optional[Any]:
    """Load the repo's structural graph; None on any failure.

    Same contract as retrieval._structural_scores / coordination's
    loader: the index root must stay OUTSIDE the original repo (the
    scan never mutates the repo — read-only by construction), and a
    missing/broken graph layer degrades to None, never an exception.
    """
    try:
        from harness.deps import get_code_graph_factory

        factory = get_code_graph_factory()
    except Exception:
        return None
    if factory is None:
        return None
    try:
        if index_root is None:
            import tempfile

            with tempfile.TemporaryDirectory(prefix="harness-scan-") as tmp:
                return factory(repo_path, root=tmp).load_or_build()
        return factory(repo_path, root=str(index_root)).load_or_build()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------


def _finding_id(kind: str, file: str, line: Optional[int], title: str) -> str:
    """Stable short id for a finding (kind + file + line + title hash)."""
    short = {"coverage_gap": "cov", "smell": "smell", "dependency": "dep"}[kind]
    slug = re.sub(r"[^a-z0-9]+", "-", Path(file).stem.lower()).strip("-")[:24]
    h = hashlib.sha1(f"{kind}|{file}|{line}|{title}".encode("utf-8")).hexdigest()[:8]
    return f"{short}-{slug or 'repo'}-{h}"


def _make_finding(
    kind: str,
    severity: str,
    title: str,
    file: str,
    line: Optional[int],
    evidence: str,
    rationale: str,
    fix_kind: str,
    fix_issue_text: str,
    fix_target_test: Optional[str],
    fix_note: str = "",
    score_extra: float = 0.0,
) -> Dict[str, Any]:
    """Assemble one finding dict (the scan.json/report/CLI shape)."""
    score = _SEVERITY_SCORE[severity] * _KIND_WEIGHT[kind] + score_extra
    return {
        "kind": kind,
        "severity": severity,
        "score": round(score, 2),
        "title": title,
        "file": file,
        "line": line,
        "evidence": evidence,
        "rationale": rationale,
        "fix_kind": fix_kind,
        "fix_issue_text": fix_issue_text,
        "fix_target_test": fix_target_test,
        "fix_note": fix_note,
        "id": _finding_id(kind, file, line, title),
    }


# ---------------------------------------------------------------------------
# Detector: coverage gaps (graph-driven — the existing structural layer)
# ---------------------------------------------------------------------------


def _detect_coverage_gaps(
    repo_path: str,
    cfg: Dict[str, Any],
    graph: Optional[Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """Modules/functions no test exercises, ranked by structural load.

    Assumes the repo is Python (the caller checks) and graph is a
    loaded memory.code_graph Graph or None (honest skip note). Module
    coverage = a test module imports it OR a test-side symbol calls any
    of its symbols (call edges — the same over-approximation the
    coordination fan-out uses, which is the right recall-over-precision
    trade for a suggestion surface). Returns findings + a note string.
    """
    if graph is None:
        return [], "code graph unavailable — coverage gaps not scanned"
    nodes = getattr(graph, "nodes", {}) or {}
    calls = getattr(graph, "calls", set()) or set()
    imports = getattr(graph, "imports", set()) or set()

    py_files = _iter_py_files(repo_path)
    test_rels = {r for r in py_files if _is_test_file(r)}
    test_mods = {_module_id_for(r) for r in test_rels}

    src_mods: Dict[str, str] = {}  # module node id -> rel file
    for nid, info in nodes.items():
        if (
            getattr(info, "kind", None) == "module"
            and getattr(info, "file", None)
            and not _excluded_path(info.file)
            and not _is_test_file(info.file)
            and not _is_scan_infra(info.file)
        ):
            src_mods[nid] = info.file

    fan_in: Counter = Counter()
    importers: Dict[str, Set[str]] = defaultdict(set)
    for src, dst in imports:
        if src in src_mods and dst in src_mods and src != dst:
            fan_in[dst] += 1
            importers[dst].add(src_mods[src])

    covered_mods: Set[str] = set()
    for src, dst in imports:
        if src in test_mods and dst in src_mods:
            covered_mods.add(dst)

    # Indirect coverage, one import hop (the coordination fan-out's
    # approximation level): a module whose ONLY importer is itself
    # tested has its behavior exercised THROUGH that importer's tests
    # — flagging it as a full gap is the false-positive class the first
    # live run exhibited (a helper imported by a tested module). Its
    # functions may still be function-level gaps if load-bearing.
    tested_files = {src_mods[m] for m in covered_mods} | test_rels
    for src, dst in imports:
        if (
            src in src_mods
            and dst in src_mods
            and src_mods[src] in tested_files
            and not _is_test_file(src_mods[dst])
        ):
            covered_mods.add(dst)

    covered_syms: Set[str] = set()
    sym_callers: Counter = Counter()
    for src, dst in calls:
        src_info = nodes.get(src)
        if src_info is not None and src_info.file in test_rels:
            covered_syms.add(dst)
    for src, dst in calls:
        src_info, dst_info = nodes.get(src), nodes.get(dst)
        if src_info is None or dst_info is None:
            continue
        if _is_test_file(src_info.file) or _excluded_path(src_info.file):
            continue
        sym_callers[dst] += 1

    syms_by_file: Dict[str, List[str]] = defaultdict(list)
    for nid, info in nodes.items():
        if (
            getattr(info, "kind", None) in ("func", "method", "class")
            and getattr(info, "file", None)
            and not _excluded_path(info.file)
            and not info.name.startswith("_")
            and not _is_test_file(info.file)
            and not _is_scan_infra(info.file)
        ):
            syms_by_file[info.file].append(nid)

    findings: List[Dict[str, Any]] = []

    # -- module-level gaps ------------------------------------------------
    for mod_nid, rel in sorted(src_mods.items()):
        if mod_nid in covered_mods:
            continue
        syms = syms_by_file.get(rel, [])
        if not syms:
            continue  # no public surface (e.g. a pure re-export __init__)
        if any(nid in covered_syms for nid in syms):
            continue  # tests exercise this module's symbols directly
        fi = fan_in.get(mod_nid, 0)
        if fi >= 2:
            sev = "high"
        elif fi == 1 or len(syms) >= 3:
            sev = "medium"
        else:
            sev = "low"
        names = sorted(nodes[nid].name for nid in syms)
        imp = sorted(importers.get(mod_nid, set()))
        imp_txt = ", ".join(imp[:3]) + (
            f" (+{len(imp) - 3} more)" if len(imp) > 3 else ""
        )
        rationale = (
            f"The module {rel} defines {len(syms)} public symbol(s) "
            f"({', '.join(names[:5])}{'…' if len(names) > 5 else ''}) but no test "
            "module imports it or calls any of them"
            + (
                f", while {fi} other module(s) depend on it ({imp_txt})"
                if fi
                else " and nothing else in the repo depends on it yet"
            )
            + ". A regression here would not fail the suite — the tests "
            "are green regardless of this module's behavior."
        )
        test_file = _suggest_test_path(rel)
        issue = (
            f"Test coverage gap found by vex scan: no test module exercises "
            f"{rel}, "
            + (
                f"which {fi} other module(s) import ({imp_txt}). "
                if fi
                else "which exports public API. "
            )
            + f"Public symbols: {', '.join(names[:8])}.\n\n"
            f"Add {test_file} covering the public symbols of {rel} — "
            "happy path plus obvious edge cases (empty input, boundary "
            "values, error conditions). The tests are the deliverable of "
            "this task: this issue explicitly authorizes adding test files."
        )
        findings.append(
            _make_finding(
                kind="coverage_gap",
                severity=sev,
                title=f"{rel}: no tests exercise this module"
                + (f" ({fi} importers)" if fi else ""),
                file=rel,
                line=None,
                evidence=(
                    f"public symbols: {', '.join(names[:6])}"
                    f"{'…' if len(names) > 6 else ''}; importers: "
                    f"{imp_txt or '(none)'}"
                ),
                rationale=rationale,
                fix_kind="fix",
                fix_issue_text=issue,
                fix_target_test=test_file,
                fix_note="the target test does not exist on the pristine "
                "tree, so the fix loop's baseline gate is honest; the "
                "issue text explicitly authorizes test-writing",
                score_extra=float(min(fi, 5)),
            )
        )

    # -- function-level gaps in covered modules (load-bearing only) ------
    covered_files = {src_mods[m] for m in covered_mods}
    func_candidates: List[Tuple[int, str, str]] = []
    for rel, syms in syms_by_file.items():
        if rel not in covered_files:
            continue
        for nid in syms:
            if nid in covered_syms:
                continue
            callers = sym_callers.get(nid, 0)
            if callers >= 2:
                func_candidates.append((callers, rel, nodes[nid].name))
    func_candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
    for callers, rel, name in func_candidates[: int(cfg.get("scan_func_gap_max", 3))]:
        test_file = _suggest_test_path(rel)
        sev = "high" if callers >= 8 else "medium"
        rationale = (
            f"{name}() in {rel} is called by {callers} place(s) in the code "
            "but never from a test; the module's other symbols are "
            "covered, so this one's contract is the unguarded seam. A "
            "behavior change here would ship without a failing test."
        )
        issue = (
            f"Test coverage gap found by vex scan: the module {rel} is "
            f"tested, but its public function {name}() has no test "
            f"exercising it ({callers} call sites in the code). Add a test "
            f"for {name}() to {test_file} — happy path plus edge cases "
            "(empty input, boundaries, error conditions). The tests are "
            "the deliverable; this issue explicitly authorizes adding "
            "test files."
        )
        findings.append(
            _make_finding(
                kind="coverage_gap",
                severity=sev,
                title=f"{rel}: {name}() has no test ({callers} call sites)",
                file=rel,
                line=None,
                evidence=f"{name}(): {callers} non-test call sites, 0 test-side calls",
                rationale=rationale,
                fix_kind="fix",
                fix_issue_text=issue,
                fix_target_test=test_file,
                fix_note="function-level gap inside an otherwise covered module",
                score_extra=float(min(callers, 10)) * 1.0,
            )
        )

    if not test_rels and findings:
        return findings, "no test files found — every module is a gap; ranked by fan-in"
    return findings, ""


def _suggest_test_path(rel: str) -> str:
    """Conventional test file path for a source module (tests/ mirror)."""
    parts = rel.split("/")
    base = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    if base == "__init__":
        base = parts[-2] if len(parts) > 1 else "module"
    return f"tests/test_{base}.py"


# ---------------------------------------------------------------------------
# Detector: code smells (stdlib AST — the real latent-bug classes)
# ---------------------------------------------------------------------------

_SMELL_RULES = {
    "mutable_default": {
        "title": "mutable default argument in {name}()",
        "rationale": (
            "Mutable default arguments are evaluated ONCE and shared across "
            "every call — state appended in one call silently leaks into "
            "the next. This exact class was a real bug in this project's "
            "own fixture suite (a cart discount accumulating across "
            "orders). The fix is an immutable default (None) created "
            "inside the body."
        ),
    },
    "bare_except": {
        "title": "bare 'except:' in {name}()",
        "rationale": (
            "A bare 'except:' also catches KeyboardInterrupt and "
            "SystemExit — control-flow exceptions get swallowed along "
            "with the error. 'except Exception:' (or a narrower type) is "
            "the safe form; a silently swallowed interrupt is a hang in "
            "production."
        ),
    },
    "except_pass": {
        "title": "silently swallowed exception in {name}()",
        "rationale": (
            "The handler discards the exception entirely ('except…: pass') "
            "— the project's own convention is 'never silently swallow a "
            "failure; log it'. At minimum record the failure; a pass "
            "makes the inevitable bug invisible when it happens."
        ),
    },
}


def _smell_visitor(rel: str, src: str) -> List[Dict[str, Any]]:
    """One file's smells as raw records (kind, lineno, name, evidence)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: List[Dict[str, Any]] = []

    def fn_name(node: ast.AST) -> str:
        return getattr(node, "name", getattr(node, "id", "<module>"))

    def mutable_default(node: ast.FunctionDef) -> Optional[int]:
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                return default.lineno
            if (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id in ("list", "dict", "set")
            ):
                return default.lineno
        return None

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            lineno = mutable_default(node)
            if lineno is not None:
                out.append(
                    {
                        "kind": "mutable_default",
                        "line": lineno,
                        "name": node.name,
                        "evidence": _line_at(src, lineno),
                    }
                )
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                out.append(
                    {
                        "kind": "bare_except",
                        "line": node.lineno,
                        "name": _enclosing_name(tree, node),
                        "evidence": _line_at(src, node.lineno),
                    }
                )
            elif all(isinstance(n, ast.Pass) for n in node.body):
                out.append(
                    {
                        "kind": "except_pass",
                        "line": node.lineno,
                        "name": _enclosing_name(tree, node),
                        "evidence": _line_at(src, node.lineno),
                    }
                )
            self.generic_visit(node)

    V().visit(tree)
    return out


def _line_at(src: str, lineno: int) -> str:
    lines = src.splitlines()
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


def _enclosing_name(tree: ast.Module, node: ast.AST) -> str:
    """Name of the function/class immediately enclosing a node.

    Walks with an explicit parent stack (ast.walk is BFS and gives no
    ancestry); returns "<module>" for top-level handlers.
    """
    stack: List[ast.AST] = [tree]
    while stack:
        cur = stack.pop()
        for child in ast.iter_child_nodes(cur):
            if child is node:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return cur.name
                if isinstance(cur, ast.ClassDef):
                    return f"{cur.name}.<unknown>"
                return _enclosing_name(tree, cur)
            stack.append(child)
    return "<module>"


def _detect_smells(
    repo_path: str,
    cfg: Dict[str, Any],
    graph: Optional[Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """AST smell pass over source .py files (tests excluded).

    Assumes repo_path is a Python repo. Only the three latent-bug
    classes with real failure histories are detected — style nits are
    deliberately out of scope (the noise budget exists to protect the
    user's attention). Per-kind findings are capped at
    scan_smells_per_kind, preferring files with structural fan-in.
    """
    per_kind_cap = int(cfg.get("scan_smells_per_kind", 3))
    fan_in = _file_fan_in(graph)
    raw: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rel in _iter_py_files(repo_path):
        if _is_test_file(rel) or _is_scan_infra(rel):
            continue
        try:
            src = Path(repo_path, rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rec in _smell_visitor(rel, src):
            raw[rec["kind"]].append({**rec, "file": rel})

    findings: List[Dict[str, Any]] = []
    for kind, recs in raw.items():
        recs.sort(key=lambda r: (-fan_in.get(r["file"], 0), r["file"], r["line"]))
        for rec in recs[:per_kind_cap]:
            sev = "low" if kind == "except_pass" else "medium"
            title = _SMELL_RULES[kind]["title"].format(name=rec["name"])
            fix = (
                "Fix it by using an immutable default (None) and creating "
                "the container inside the function body"
                if kind == "mutable_default"
                else "Catch 'Exception' or a narrower type and at least log the failure"
            )
            test_file = _suggest_test_path(rec["file"])
            issue = (
                f"Code smell found by vex scan at {rec['file']}:{rec['line']} — "
                f"{_SMELL_RULES[kind]['title'].format(name=rec['name'])}.\n"
                f"Offending line: {rec['evidence']}\n\n{fix}. Then add a "
                f"regression test as {test_file} that would have caught this "
                "class (for a mutable default: call the function twice and "
                "assert no state leaks between calls; for a swallowed "
                "exception: assert the failure is observable, not silent). "
                "This issue explicitly authorizes adding test files."
            )
            findings.append(
                _make_finding(
                    kind="smell",
                    severity=sev,
                    title=f"{rec['file']}:{rec['line']} — {title}",
                    file=rec["file"],
                    line=rec["line"],
                    evidence=rec["evidence"],
                    rationale=_SMELL_RULES[kind]["rationale"],
                    fix_kind="fix",
                    fix_issue_text=issue,
                    fix_target_test=test_file,
                    fix_note="repro-test-first shape through the fix loop",
                    score_extra=float(min(fan_in.get(rec["file"], 0), 4)) * 0.5,
                )
            )
    if findings:
        note = f"{sum(len(v) for v in raw.values())} smell site(s) found; showing top {len(findings)} by structural load"
    else:
        note = ""
    return findings, note


def _file_fan_in(graph: Optional[Any]) -> Dict[str, int]:
    """Rel-file -> number of other source files importing it (graph)."""
    if graph is None:
        return {}
    nodes = getattr(graph, "nodes", {}) or {}
    imports = getattr(graph, "imports", set()) or set()
    file_of: Dict[str, str] = {}
    for nid, info in nodes.items():
        if (
            getattr(info, "kind", None) == "module"
            and getattr(info, "file", None)
            and not _excluded_path(info.file)
            and not _is_test_file(info.file)
        ):
            file_of[nid] = info.file
    fan: Counter = Counter()
    for src, dst in imports:
        if src in file_of and dst in file_of and src != dst:
            fan[file_of[dst]] += 1
    return dict(fan)


# ---------------------------------------------------------------------------
# Detector: dependencies (manifests; opt-in PyPI freshness)
# ---------------------------------------------------------------------------

_DEP_SPEC = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
    r"(?:\[[^]]+\])?\s*(?P<op>==|>=|<=|~=|!=|>|<)?\s*(?P<ver>[^\s;,#]+)?"
)


def _parse_manifests(repo_path: str) -> Dict[str, List[Tuple[str, str, str]]]:
    """Pinned/equality deps from requirements.txt + pyproject.

    Returns {name_lower: [(name, spec, manifest), ...]} — duplicates
    across manifests stay in the list (a same-name entry in two
    manifests with different pins is a real finding). Best-effort:
    malformed lines are skipped, never raised.
    """
    out: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    root = Path(repo_path)
    req = root / "requirements.txt"
    if req.is_file():
        try:
            for raw in req.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith(("-r", "-e", "--")):
                    continue
                m = _DEP_SPEC.match(line)
                if m and m.group("op") == "==":
                    out[m.group("name").lower()].append(
                        (m.group("name"), m.group("ver") or "", "requirements.txt")
                    )
                elif m:
                    out[m.group("name").lower()].append(
                        (m.group("name"), "", "requirements.txt")
                    )
        except OSError:
            pass
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        deps = _pyproject_dependencies(pyproject)
        for entry in deps:
            m = _DEP_SPEC.match(entry)
            if m:
                out[m.group("name").lower()].append(
                    (m.group("name"), m.group("ver") or "", "pyproject.toml")
                )
    return dict(out)


def _pyproject_dependencies(pyproject: Path) -> List[str]:
    """[project].dependencies entries, tomllib-first with a regex fallback.

    The fallback handles both multiline arrays (one dep per line) and
    single-line inline arrays — a 3.10 interpreter has no tomllib and
    must still parse the common shapes.
    """
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        import tomllib  # Python 3.11+

        data = tomllib.loads(text)
        deps = (data.get("project") or {}).get("dependencies") or []
        return [str(d) for d in deps if isinstance(d, str)]
    except ImportError:
        pass
    except Exception:
        return []
    m = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
    if not m:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))


def _version_tuple(v: str) -> Tuple[Tuple[int, str], ...]:
    """Comparable tuple for a loose PEP-440-ish version string."""
    v = v.strip().strip("'\"")
    toks = re.split(r"[.-]", re.sub(r"[^0-9A-Za-z.\-]", "", v))
    out = []
    for t in toks:
        if t.isdigit():
            out.append((0, t))
        elif t:
            out.append((1, t.lower()))
    return tuple(out)


def _pypi_latest(pkg: str, timeout_s: int) -> Optional[str]:
    """Latest version string from the PyPI JSON API (GET-only, fixed
    endpoint, same guard pattern as docs_lookup._pypi_lookup). None on
    any failure — an offline/unknown package degrades to a note, never
    an exception."""
    if not pkg or not re.match(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$", pkg):
        return None
    try:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{pkg}/json",
            headers={"User-Agent": "vex-harness-scan/1.0"},
        )
        with urllib.request.urlopen(req, timeout=max(3, timeout_s)) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return str(((data or {}).get("info") or {}).get("version") or "") or None
    except Exception:
        return None


def _detect_dependencies(
    repo_path: str,
    cfg: Dict[str, Any],
    remote: bool,
) -> Tuple[List[Dict[str, Any]], str]:
    """Pin conflicts (offline) + outdated pins (opt-in PyPI check).

    Assumes repo_path is a readable directory. Offline finds local
    inconsistencies only; the PyPI freshness check runs when remote is
    True (CLI --remote / config scan_remote_deps) — network stays
    opt-in per the project's docs_lookup precedent. "Known issues" are
    NOT claimed: the honest signal is version distance (a major-
    version-behind pin stops receiving fixes), and the rationale says
    exactly that.
    """
    by_name = _parse_manifests(repo_path)
    if not by_name:
        return (
            [],
            "no dependency pins found (no == requirements, no [project] dependencies)",
        )

    findings: List[Dict[str, Any]] = []
    notes: List[str] = []

    # -- cross-manifest conflicts (offline, local fact) -------------------
    for _name_l, entries in sorted(by_name.items()):
        manifests = {mf for (_, _, mf) in entries}
        if len(manifests) > 1:
            specs = {(nm, spec, mf) for (nm, spec, mf) in entries if spec}
            if len(specs) > 1:
                detail = ", ".join(
                    f"{s} in {mf}" for (_, s, mf) in sorted(specs, key=lambda t: t[2])
                )
                rationale = (
                    f"The dependency {entries[0][0]} is pinned differently "
                    f"across manifests ({detail}). Installers resolve one "
                    "silently, so the environment is not what one of the "
                    "files claims — this class produces 'works on my "
                    "machine' bugs that no test can catch."
                )
                findings.append(
                    _make_finding(
                        kind="dependency",
                        severity="medium",
                        title=f"{entries[0][0]}: conflicting pins across manifests",
                        file=sorted(manifests)[0],
                        line=None,
                        evidence=detail,
                        rationale=rationale,
                        fix_kind="fix",
                        fix_issue_text=(
                            f"Dependency conflict found by vex scan: {entries[0][0]} "
                            f"is pinned inconsistently ({detail}). Align the pin "
                            "across every manifest to one version, then run the "
                            "full test suite to confirm the chosen version."
                        ),
                        fix_target_test=None,
                        fix_note="suite-gated: the deliverable is the aligned pin",
                    )
                )

    # -- outdated pins (opt-in remote) -------------------------------------
    checked = 0
    if remote:
        for _name_l, entries in sorted(by_name.items()):
            pinned = [(nm, spec, mf) for (nm, spec, mf) in entries if spec]
            if not pinned or checked >= 40:
                continue
            checked += 1
            latest = _pypi_latest(pinned[0][0], int(cfg.get("scan_pypi_timeout_s", 10)))
            if not latest:
                continue
            for nm, spec, mf in pinned:
                cur = spec.lstrip("=~<>!")
                lt, lv = _version_tuple(cur), _version_tuple(latest)
                if not lt or not lv or lt >= lv:
                    continue
                cur_major = lt[0][1] if lt[0][0] == 0 and lt[0][1].isdigit() else None
                latest_major = (
                    lv[0][1] if lv[0][0] == 0 and lv[0][1].isdigit() else None
                )
                major_gap = (
                    cur_major is not None
                    and latest_major is not None
                    and latest_major != cur_major
                )
                sev = (
                    "high"
                    if major_gap
                    else ("medium" if len(lv) > 1 and lv[:2] > lt[:2] else "low")
                )
                dist = (
                    f"a major version behind (pinned {cur_major}.x, latest "
                    f"{latest_major}.x)"
                    if major_gap
                    else "behind the latest line"
                )
                rationale = (
                    f"{nm} is pinned {spec} in {mf} while PyPI's latest is "
                    f"{latest} ({dist}). Older lines stop receiving bug and "
                    "security fixes — the longer the drift, the more known "
                    "issues the pin accumulates and the harder the eventual "
                    "jump becomes."
                )
                findings.append(
                    _make_finding(
                        kind="dependency",
                        severity=sev,
                        title=f"{nm} {spec} is outdated (latest {latest})",
                        file=mf,
                        line=None,
                        evidence=f"{mf}: {nm}{spec} · PyPI latest {latest}",
                        rationale=rationale,
                        fix_kind="build",
                        fix_issue_text=(
                            f"Outdated dependency found by vex scan: {nm} is "
                            f"pinned {spec} in {mf} but the latest release is "
                            f"{latest}. Upgrade the pin to a current release, "
                            "make any compatibility changes the new version "
                            "requires, and ensure the full test suite still "
                            "passes. The acceptance test should assert the "
                            f"installed version of {nm} meets the new floor "
                            f"(>= {latest})."
                        ),
                        fix_target_test=None,
                        fix_note="build mode: the version-floor acceptance "
                        "test genuinely fails on the old pin",
                    )
                )
        if checked:
            notes.append(f"PyPI freshness checked for {checked} pinned package(s)")
    else:
        unpinned = sum(1 for es in by_name.values() for e in es if not e[1])
        notes.append(
            "dependencies: offline — pass --remote (or scan_remote_deps) "
            "to check pins against PyPI"
        )
        if unpinned:
            notes.append(f"{unpinned} manifest entrie(s) have no version pin")
    return findings, "; ".join(notes)


# ---------------------------------------------------------------------------
# Ranking (Task B) + report rendering
# ---------------------------------------------------------------------------


def rank_findings(
    findings: List[Dict[str, Any]], max_findings: int
) -> Tuple[List[Dict[str, Any]], int]:
    """Score-sort findings and stamp 1-based rank indexes.

    Assumes findings are dicts from the detectors (score already
    present). Returns (ranked, suppressed) where ranked carries ALL
    findings in value order with ``index`` stamped — the REPORT shows
    only the top max_findings; scan.json keeps everything so
    ``--max-findings`` can resurface without rescanning.
    """
    ranked = sorted(
        findings,
        key=lambda f: (-f["score"], f["kind"], f["file"], f.get("line") or 0),
    )
    for i, f in enumerate(ranked, start=1):
        f["index"] = i
    return ranked, max(0, len(ranked) - max_findings)


def _wrap(text: str, width: int = 88, indent: str = "  ") -> List[str]:
    """Simple word-wrap for terminal/report rendering (cp1252-safe)."""
    out: List[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            if cur and len(cur) + 1 + len(word) > width:
                out.append(indent + cur)
                cur = word
            else:
                cur = word if not cur else cur + " " + word
        if cur:
            out.append(indent + cur)
    return out


def render_report(scan: Dict[str, Any]) -> str:
    """Human/markdown report body for a completed scan (report.md +
    terminal share this text). Deterministic from the scan dict."""
    lines: List[str] = []
    lines.append(f"# Vex scan — {scan['repo_path']}")
    shown = scan["findings"][: scan["shown"]]
    lines.append(
        f"scan {scan['scan_id']} · {len(scan['findings'])} finding(s) · "
        f"{len(shown)} shown · {scan['suppressed']} suppressed"
    )
    lines.append("")
    if not shown:
        lines.append(
            "No findings worth attention. "
            + ("; ".join(scan["notes"]) if scan["notes"] else "")
        )
    for f in shown:
        lines.append(f"## {f['index']}. [{f['severity']}] {f['kind']} — {f['title']}")
        loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
        lines.append(f"- location: {loc}")
        if f.get("evidence"):
            lines.append(f"- evidence: {f['evidence']}")
        lines.append("")
        lines.append(f["rationale"])
        lines.append("")
        lines.append(
            f"Fix as a task: `vex fix --finding {scan['scan_id']}#{f['index']}`"
        )
        lines.append("")
    if scan["suppressed"]:
        lines.append(
            f"({scan['suppressed']} lower-value finding(s) not shown — "
            f"`vex scan --max-findings {max(scan['shown'], len(scan['findings']))}` "
            "or read scan.json to see them)"
        )
        lines.append("")
    for n in scan["notes"]:
        lines.append(f"note: {n}")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Public entry (Task A/B) + Task-C handoff
# ---------------------------------------------------------------------------


def run_scan(
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
    remote: Optional[bool] = None,
    focus: Optional[str] = None,
    max_findings: Optional[int] = None,
) -> Dict[str, Any]:
    """Scan a repo read-only; return the ranked findings + report paths.

    Returns {"status", "scan_id", "repo_path", "findings" (ranked, ALL
    of them, ``index``-stamped), "shown", "suppressed", "notes",
    "counts", "report_path", "scan_path", "trace_path"}. status is
    "error" only when the repo is unreadable — findings are data, not
    failure. Assumes repo_path is intended as a repo directory and
    config is the session/task config dict (unknown keys pass through).
    NEVER mutates the repo: file reads, the graph index OUTSIDE the
    repo, and log writes under log_root only. No model calls, no
    sandbox, no shell — a scan is deterministic and offline by default
    (remote=True enables the opt-in PyPI freshness check).
    """
    cfg = get_config(config or {})
    tid = task_id or f"scan-{uuid.uuid4().hex[:8]}"
    root = Path(log_root) if log_root else Path(cfg.get("work_subdir", "logs"))
    trace = TraceLogger(root / tid)

    repo_abs = str(Path(repo_path).resolve())
    trace.log(
        "task_start",
        {
            "task_id": tid,
            "mode": "scan",
            "repo_path": repo_abs,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )

    if not Path(repo_path).is_dir():
        trace.log("task_end", {"status": "error", "reason": "repo is not a directory"})
        return {
            "status": "error",
            "scan_id": tid,
            "repo_path": repo_abs,
            "findings": [],
            "shown": 0,
            "suppressed": 0,
            "notes": ["repo is not a directory"],
            "counts": {},
            "report_path": "",
            "scan_path": "",
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
        }

    py_files = _iter_py_files(repo_path)
    graph = _load_graph(repo_path, root / "_code-graph") if py_files else None
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []

    if not py_files:
        notes.append(
            "no Python source files found — coverage/smell detectors are "
            "Python-first; dependency manifests still scanned"
        )
    if py_files and focus in (None, "", "coverage"):
        cov, note = _detect_coverage_gaps(repo_path, cfg, graph)
        findings.extend(cov)
        if note:
            notes.append(note)
        trace.log(
            "scan_detector", {"detector": "coverage", "found": len(cov), "note": note}
        )
    if py_files and focus in (None, "", "smells"):
        sm, note = _detect_smells(repo_path, cfg, graph)
        findings.extend(sm)
        if note:
            notes.append(note)
        trace.log(
            "scan_detector", {"detector": "smells", "found": len(sm), "note": note}
        )
    if focus in (None, "", "dependencies"):
        # Manifest-level analysis is language-independent: it runs on
        # any repo shape (a JS repo with a requirements.txt is odd but
        # reportable), unlike the AST/graph detectors.
        use_remote = (
            bool(cfg.get("scan_remote_deps", False)) if remote is None else bool(remote)
        )
        dep, note = _detect_dependencies(repo_path, cfg, use_remote)
        findings.extend(dep)
        if note:
            notes.append(note)
        trace.log(
            "scan_detector",
            {
                "detector": "dependencies",
                "found": len(dep),
                "note": note,
                "remote": use_remote,
            },
        )

    cap = int(
        max_findings if max_findings is not None else cfg.get("scan_max_findings", 8)
    )
    ranked, suppressed = rank_findings(findings, cap)
    counts = {
        "by_kind": dict(Counter(f["kind"] for f in ranked)),
        "by_severity": dict(Counter(f["severity"] for f in ranked)),
    }

    scan = {
        "status": "success",
        "scan_id": tid,
        "repo_path": repo_abs,
        "ts": round(time.time(), 3),
        "config_snapshot": {k: v for k, v in cfg.items() if k != "api_key"},
        "focus": focus or "all",
        "findings": ranked,
        "shown": min(cap, len(ranked)),
        "suppressed": suppressed,
        "notes": notes,
        "counts": counts,
        "report_path": str((root / tid / "report.md").resolve()),
        "scan_path": str((root / tid / "scan.json").resolve()),
        "trace_path": str((root / tid / "trace.jsonl").resolve()),
    }

    scan_dir = root / tid
    try:
        scan_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            root / tid / "scan.json", json.dumps(scan, indent=2, ensure_ascii=False)
        )
        _atomic_write(root / tid / "report.md", render_report(scan))
    except OSError as exc:
        scan["notes"] = [*notes, f"persisting scan artifacts failed: {exc}"]

    trace.log(
        "scan_summary",
        {
            "findings": len(ranked),
            "shown": scan["shown"],
            "suppressed": suppressed,
            "counts": counts,
        },
    )
    trace.log("task_end", {"status": "success"})
    return scan


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _latest_scan_dir(log_root: Path) -> Optional[Path]:
    """Newest scan-<id> dir under log_root (for `vex scan --fix N`)."""
    if not log_root.is_dir():
        return None
    cands = [p for p in log_root.iterdir() if p.is_dir() and p.name.startswith("scan-")]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def resolve_finding(
    ref: str,
    log_root: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """Resolve "<scan_id>#<n>" (Task C) to (scan_meta, finding, error).

    Assumes ref is user input and log_root is the scans' root. The
    scan id runs through the SAME shared guard as task ids
    (memory.paths.safe_task_dir) — traversal-shaped ids are rejected
    before any filesystem use. Index n is 1-based against the scan's
    ranked findings (the exact order printed in the report). Returns
    (scan, finding, "") on success or (None, None, reason) on failure.
    """
    ref = (ref or "").strip()
    m = re.match(r"^(scan-[A-Za-z0-9_-]+)#(\d+)$", ref)
    if not m:
        return (
            None,
            None,
            (
                "finding ref must look like scan-<id>#<n> "
                "(printed by `vex scan` next to each finding)"
            ),
        )
    scan_id, n = m.group(1), int(m.group(2))
    try:
        from memory.paths import safe_task_dir
    except ImportError:
        scan_dir = log_root / scan_id
        if ".." in scan_id or "/" in scan_id or "\\" in scan_id:
            return None, None, "invalid scan id"
    else:
        scan_dir = safe_task_dir(scan_id, log_root)
    if scan_dir is None or not (scan_dir / "scan.json").is_file():
        return None, None, f"no scan found for {scan_id!r} under {log_root}"
    try:
        data = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, None, f"scan.json unreadable: {exc}"
    findings = data.get("findings") or []
    if n < 1 or n > len(findings):
        return None, None, f"finding index {n} out of range (1..{len(findings)})"
    return data, findings[n - 1], ""


def finding_task_params(finding: Dict[str, Any]) -> Dict[str, Any]:
    """The Task-C handoff contract: finding -> task parameters.

    Returns {"issue_text", "target_test", "fix_kind", "note"}. fix_kind
    "fix" runs through core.run_task (a suggested test file is the
    target — it must NOT exist pre-fix, so the baseline verify fails
    honestly and the loop's don't-touch-tests rule is explicitly
    authorized by the issue text); fix_kind "build" runs through
    build_mode.run_build (a version-floor acceptance test genuinely
    fails on the old state).
    """
    return {
        "issue_text": str(finding.get("fix_issue_text") or finding.get("title") or ""),
        "target_test": finding.get("fix_target_test") or None,
        "fix_kind": str(finding.get("fix_kind") or "fix"),
        "note": str(finding.get("fix_note") or ""),
    }
