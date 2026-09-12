"""Coordinated multi-file change detection (Improvement Round 2, Task A).

A fix in one file often REQUIRES matching changes elsewhere — a method
signature change (a flag removed) implies updates at every call site; a
rename implies updates across the definition and all its callers; a
field rename implies updates across the model and its serializers.
This module recognizes when that is the situation, using the STRUCTURAL
code graph (Terminal 4's memory.code_graph — call edges and import
edges), not just grep: a call site that never mentions the old name in
textual form (``obj.invoice_total(...)``) is still found through the
call edge from the caller to the method.

Two cooperating pieces:

1. PLANNING-side fan-out (`detect_coordinated_change`): given the
   changed files so far (diff vs pristine) or the plan's files_hints,
   walk the graph from every symbol DEFINED in a changed file to its
   callers and the changed modules' importers — the set of files whose
   code depends on what changed. A change with a NON-EMPTY dependents
   set and the issue/plan signaling a signature/rename shape is a
   COORDINATED change: all dependents must be updated together or none
   should land. Result drives the planner prompt (a `change_group` is
   suggested) and trace/decision observability. Never raises; without
   the graph layer the result degrades to ``{"detected": False,
   "reason": "graph unavailable"}`` — single-file fixes behave exactly
   as before.

2. VERIFICATION-side group gate (`missing_group_members` / `format_*
   helpers`): given the group's declared file set and the actually-
   changed set, compute which required members are missing — the
   atomicity check. The loop controller (core.run_task) uses it after
   an attempt's steps complete: an attempt that edited only 1 of 4
   group files is a PARTIAL coordinated change — poisoned attempt with
   explicit feedback (or a group rollback), never allowed to reach the
   verifier as if it were complete.

Config (task.config, defaults in harness/config.py):
  coordination_detect   master switch for the planning-side detection
  coordination_min_files  a group needs >= 2 files to be a "group"
  change_groups          (plan schema) step field binding steps to a
                        named atomic group — see prompts.py + context.py
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Vocabulary: does the issue text signal a coordinated-change shape?
# ---------------------------------------------------------------------------

# A signature change / removal (arity or flag change at call sites).
_SIG_PAT = re.compile(
    r"\b(signature|parameter|argument|arg list|arity|"
    r"drop(ping|ped)?\s+(the\s+)?\w*\s*(flag|param(eter)?)|"
    r"remov(e|ing|ed)\s+(the\s+)?\w*\s*(flag|param(eter)?)|"
    r"no longer (takes|accepts|needs)|keyword (argument|param(eter)?))\b",
    re.IGNORECASE,
)
# A rename shape (identifier renamed across the codebase).
_RENAME_PAT = re.compile(
    r"\b(rename[d]?|renaming|call sites?|every (caller|call|use|usage)|"
    r"all (callers|call sites|uses|usages|consumers|dependents)|"
    r"update[d]? (all )?(callers|call sites|uses|usages|consumers)|"
    r"each (call site|caller)|references? (to|of))\b",
    re.IGNORECASE,
)


def coordination_kind(
    issue_text: str, plan_texts: Optional[List[str]] = None
) -> Optional[str]:
    """Classify the coordinated-change shape signaled by the text.

    Returns "signature" (a signature/flag/arity change), "rename"
    (identifier rename with call-site updates), or None when neither
    vocabulary fires. Assumes issue_text is the bug report and
    plan_texts are optional planner step descriptions / checkpoints to
    include in the scan (the plan usually names the rename once the
    planner has seen the fan-out). Pure string classification — never
    touches the graph, never raises.
    """
    hay = " \u2028 ".join([issue_text or "", *list(plan_texts or [])])
    if not hay.strip():
        return None
    if _SIG_PAT.search(hay):
        return "signature"
    if _RENAME_PAT.search(hay):
        return "rename"
    return None


# ---------------------------------------------------------------------------
# Planning-side fan-out: changed files -> structurally dependent files
# ---------------------------------------------------------------------------


def _graph_for(repo_path: str, index_root: Optional[Path]) -> Optional[Any]:
    """Load (or build) the repo's structural graph; None on any failure.

    Same contract as retrieval._structural_scores: the index root must
    stay OUTSIDE the original repo (the harness never mutates it), and
    a missing/broken graph layer degrades to None — coordination
    detection must not kill a task run.
    """
    factory = None
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

            with tempfile.TemporaryDirectory(prefix="harness-coord-") as tmp:
                obj = factory(repo_path, root=tmp)
                return obj.load_or_build()
        obj = factory(repo_path, root=str(index_root))
        return obj.load_or_build()
    except Exception:
        return None


def _files_of(graph: Any, node_ids: Set[str]) -> Set[str]:
    out: Set[str] = set()
    nodes = getattr(graph, "nodes", {}) or {}
    for nid in node_ids:
        info = nodes.get(nid)
        if info is not None and info.file:
            out.add(info.file)
    return out


def dependent_files(
    graph: Any,
    changed_files: List[str],
    max_files: int = 12,
) -> Dict[str, List[str]]:
    """Files structurally depending on symbols defined in changed_files.

    Fan-out is TWO kinds of edge, both from the graph (not grep):
    - CALL edges: every caller of a func/method/class defined in a
      changed file (a signature change breaks each of these; a rename
      requires updating each of them).
    - IMPORT edges: every module importing a changed file's module
      (keeps the fan-out honest for uses the call graph's name-based
      resolution misses).

    Returns {dependent_file: [symbols forming the dependency]} — the
    symbol names give the planner feedback concrete anchors. Assumes
    graph is a loaded memory.code_graph Graph and changed_files are
    repo-relative posix paths. Bounded at max_files dependents.
    """
    out: Dict[str, List[str]] = {}
    if not changed_files:
        return out
    changed_set = {c.replace("\\", "/") for c in changed_files}
    nodes = getattr(graph, "nodes", {}) or {}
    calls = getattr(graph, "calls", set()) or set()
    imports = getattr(graph, "imports", set()) or set()

    # Symbols defined in the changed files (any kind with a file).
    defined_here: Set[str] = {
        nid
        for nid, info in nodes.items()
        if info is not None and info.file in changed_set
    }
    # Module nodes of the changed files' modules.
    changed_mods: Set[str] = {
        nid
        for nid, info in nodes.items()
        if info is not None and info.kind == "module" and info.file in changed_set
    }

    # 1. Call-edge fan-out: caller symbol -> callee defined in changed.
    for src, dst in calls:
        if dst in defined_here and src in nodes:
            dep_file = nodes[src].file
            if dep_file not in changed_set:
                out.setdefault(dep_file, [])
                if nodes[dst].name not in out[dep_file]:
                    out[dep_file].append(nodes[dst].name)

    # 2. Import-edge fan-out: importer module of a changed module.
    for src, dst in imports:
        if dst in changed_mods and src in nodes:
            dep_file = nodes[src].file
            if dep_file not in changed_set:
                out.setdefault(dep_file, [])
                # No symbol anchor for import edges; name the module.
                mod_name = nodes[dst].name
                if mod_name not in out[dep_file]:
                    out[dep_file].append(mod_name)

    bounded: Dict[str, List[str]] = {}
    for rel in sorted(out):
        if len(bounded) >= max_files:
            break
        bounded[rel] = out[rel]
    return bounded


def detect_coordinated_change(
    repo_path: str,
    issue_text: str,
    changed_files: Optional[List[str]] = None,
    plan_texts: Optional[List[str]] = None,
    index_root: Optional[Path] = None,
    kind: Optional[str] = None,
    max_files: int = 12,
    protected_patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Decide whether this fix is a coordinated multi-file change.

    Assumes repo_path is the ORIGINAL repo (read-only — the graph index
    lives under index_root, never inside the repo), issue_text the bug
    report, changed_files the files edited so far or the plan's
    files_hints (repo-relative posix), and plan_texts optional planner
    step descriptions. protected_patterns (editor.is_protected globs)
    exclude protected files (tests, config-pinned paths) from the
    SUGGESTED group — an atomic agent-edit group can never legitimately
    include a protected path, and suggesting one would force the gate
    into impossible rejections. Returns a JSON-serializable dict:

    {
      "detected": bool,          # a coordinated change is recognized
      "kind": str|None,          # "signature" | "rename" | None
      "reason": str,             # human-readable, always present
      "changed_files": [...],    # the input files (normalized, filtered)
      "dependent_files": {...},  # {file: [symbol anchors]} fan-out
      "group_files": [...],      # suggested ATOMIC group (changed + deps)
      "excluded": [...],         # fan-out files dropped (protected)
      "strategy": "structural",  # detection used the graph
    }

    Never raises. Without the graph (or with no changed files to fan
    out from), detected=False with an explanatory reason — callers run
    exactly the pre-coordination flow.
    """
    changed = [c.replace("\\", "/") for c in (changed_files or [])]
    if kind is None:
        kind = coordination_kind(issue_text, plan_texts)

    excluded: List[str] = []
    if protected_patterns:
        kept: List[str] = []
        for c in changed:
            if is_protected_path(c, protected_patterns):
                excluded.append(c)
            else:
                kept.append(c)
        changed = kept

    base: Dict[str, Any] = {
        "detected": False,
        "kind": kind,
        "reason": "",
        "changed_files": changed,
        "dependent_files": {},
        "group_files": [],
        "excluded": excluded,
        "strategy": "structural",
    }
    if not changed:
        base["reason"] = "no changed files to fan out from"
        return base

    graph = _graph_for(repo_path, index_root)
    if graph is None:
        base["reason"] = "graph unavailable"
        base["strategy"] = "none"
        return base

    deps = dependent_files(graph, changed, max_files=max_files)
    if protected_patterns:
        for rel in list(deps):
            if is_protected_path(rel, protected_patterns):
                excluded.append(rel)
                del deps[rel]
    base["excluded"] = sorted(set(excluded))
    base["dependent_files"] = deps
    base["group_files"] = sorted(set(changed) | set(deps))

    if not deps:
        base["reason"] = "no structural dependents — single-file change is safe to land"
        return base

    if kind is None:
        base["reason"] = (
            f"{len(deps)} structurally dependent file(s), but the issue/"
            "plan text signals no signature/rename shape — flagging for "
            "group planning anyway (dependents exist)"
        )
        base["detected"] = True
        return base

    base["detected"] = True
    shape = {
        "signature": "signature change requires updates at every call site",
        "rename": "rename requires updates across definition and callers",
    }.get(kind, "coordinated change")
    base["reason"] = (
        f"{shape}; {len(deps)} dependent file(s) found via call/import "
        f"edges: {', '.join(sorted(deps))}"
    )
    return base


# ---------------------------------------------------------------------------
# Verification-side atomicity gate (used by core.run_task)
# ---------------------------------------------------------------------------


def is_protected_path(rel_path: str, protected_patterns: List[str]) -> bool:
    """Protected-path check reused from editor (is_protected semantics,
    including always-protected VCS dirs) — coordination must never
    suggest a group member the edit policy forbids touching. Assumes
    rel_path is repo-relative posix and protected_patterns are the
    task's protected_paths globs."""
    try:
        from harness.editor import is_protected

        return is_protected(rel_path, protected_patterns or [])
    except Exception:
        return False  # never break detection over a policy lookup


def missing_group_members(
    group_files: List[str],
    changed_files: List[str],
) -> List[str]:
    """Required group members NOT present in the actually-changed set.

    The atomicity check: a coordinated change declared as touching
    group_files must show every member in changed_files (deleted files
    count — deleting IS a change). Assumes both lists are repo-relative
    posix paths; comparison is normalized. A member counts as present
    when it appears in changed_files exactly (the group contract is
    declared by the planner over real paths).
    """
    changed_set = {c.replace("\\", "/") for c in (changed_files or [])}
    return [g for g in (group_files or []) if g.replace("\\", "/") not in changed_set]


def format_coordination_block(detection: Dict[str, Any]) -> str:
    """Render the planner-prompt section for a detected coordinated change.

    Assumes detection is detect_coordinated_change output. Returns ""
    when nothing was detected (the planner prompt is unchanged for
    single-file fixes).
    """
    if not detection.get("detected"):
        return ""
    deps = detection.get("dependent_files") or {}
    lines = [
        "## Coordinated multi-file change detected",
        f"- Shape: {detection.get('kind') or 'cross-file dependency'} — "
        f"{detection.get('reason', '')}",
        "- Files already changing: " + ", ".join(detection.get("changed_files") or []),
        "- Files that structurally depend on them (call/import edges):",
    ]
    for rel in sorted(deps):
        syms = ", ".join(deps[rel][:4])
        more = f" (+{len(deps[rel]) - 4} more)" if len(deps[rel]) > 4 else ""
        lines.append(f"  - {rel} via {syms}{more}")
    lines.append(
        "This change is ATOMIC: either every dependent file above is "
        "updated in the same change, or the change must not land. Plan "
        "the call-site updates as part of the SAME logical step, or use "
        "the SAME `change_group` name on each step that edits these "
        "files, so the harness can validate and roll them back as ONE "
        "unit."
    )
    return "\n".join(lines)


def format_missing_group_feedback(
    group_files: List[str],
    missing: List[str],
) -> str:
    """The retry feedback for a PARTIAL coordinated change.

    Assumes missing is missing_group_members output (non-empty) and
    group_files the declared group. Names the missed files so the next
    attempt's planner/step sessions know exactly what to complete.
    """
    return (
        "The coordinated change is INCOMPLETE: it was declared to touch "
        f"{len(group_files)} files as one atomic unit "
        f"({', '.join(group_files)}), but {len(missing)} required "
        f"member(s) were never changed: {', '.join(missing)}. Either "
        "complete the missing updates (the change's dependents must be "
        "updated together with it) or revert the whole group — a partial "
        "coordinated change must not land."
    )
