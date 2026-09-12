"""Planner-time decision-memory queries (Terminal 1 + Terminal 4 joint work).

Before the planner call, the harness asks Terminal 4's decision store
"what did we learn in THIS repo before?" and injects the answer as a
planner-prompt section (harness.prompts' `## Relevant past decisions`).
This closes the loop the memory layer was built for: decisions were
recorded and queryable, but nothing consumed them at planning time —
they only landed in the store after tasks finished.

Query strategy (keyword, no embeddings — same honesty as the rest of
the stack): the retrieval terms already extracted from the issue text
(retrieval.extract_terms) plus the repo's path-ish segments. Ranking is
the store's own (matched-word count, then recency). The query is scoped
to the current repo via DecisionStore.search's repo_path filter —
decisions from other repos are noise for planning this one.

Everything here is best-effort BY DESIGN: a missing memory module, an
unreadable store, or a broken query degrades to "no decisions" and the
planner runs exactly as before (trace event, never a crash — planning
must not die because memory is down).

Config (task.config, defaults in harness/config.py):
  plan_with_memory   master switch (False = the ablation OFF arm)
  memory_query_limit max decisions injected
  memory_max_chars   combined char cap on the section
"""

from typing import Any, Dict, List, Optional

from harness import deps


def query_planning_decisions(
    repo_path: str,
    issue_text: str,
    retrieval_terms: Optional[List[str]] = None,
    limit: int = 6,
) -> Dict[str, Any]:
    """Query decision memory for planning this task.

    Assumes repo_path is the task's repo (same value the harness records
    as state.json repo_path, so the store's repo filter matches) and
    issue_text is the task's bug report. Returns {"decisions": [str],
    "query": str, "error": Optional[str]} — decisions are the rendered
    section lines (text + origin), already limited. Never raises; any
    failure returns decisions=[] with error set.
    """
    out: Dict[str, Any] = {"decisions": [], "query": "", "error": None}
    factory = deps.get_decision_store_factory()
    if factory is None:
        out["error"] = "memory module unavailable"
        return out
    try:
        store = factory()
    except Exception as exc:  # store open failure — degrade, don't crash
        out["error"] = f"decision store open failed: {exc}"
        return out

    terms = list(retrieval_terms or [])
    if not terms:
        from harness.retrieval import extract_terms

        terms = extract_terms(issue_text)
    query = _query_from(repo_path, terms)
    out["query"] = query
    try:
        results = store.search(query, limit=max(1, int(limit)), repo_path=repo_path)
    except Exception as exc:  # never raise into the planning path
        out["error"] = f"decision query failed: {exc}"
        return out
    try:
        store.close()
    except Exception:
        pass  # close is best-effort; WAL keeps the file consistent
    out["decisions"] = [_render_decision(d) for d in results if _render_decision(d)]
    return out


def render_memory_block(decisions: List[str], max_chars: int = 1500) -> str:
    """Render the planner-prompt section body from decision lines.

    Assumes each entry of `decisions` is one rendered decision line (from
    query_planning_decisions). Caps the section at max_chars with an
    explicit truncation marker; empty input renders as an explicit
    "(none recorded yet)" so the model knows memory was consulted and
    had nothing, rather than wondering whether the section was omitted.
    """
    if not decisions:
        return "(none recorded yet)"
    lines: List[str] = []
    used = 0
    for d in decisions:
        if used + len(d) > max_chars and lines:
            lines.append(f"... [{len(decisions) - len(lines)} more truncated]")
            break
        lines.append(f"- {d}")
        used += len(d) + 2
    return "\n".join(lines)


def _query_from(repo_path: str, terms: List[str]) -> str:
    """Search text for the store: issue terms + the repo's own path/name
    segments (decisions about the repo's conventions often mention the
    package/module names, which the issue text may not)."""
    from pathlib import PurePosixPath
    import re as _re

    parts: List[str] = []
    for seg in str(repo_path).replace("\\", "/").split("/"):
        for word in _re.findall(r"[A-Za-z0-9_]+", seg):
            parts.append(word)
    pkg = PurePosixPath(str(repo_path).replace("\\", "/")).name
    seen: List[str] = []
    for t in parts + list(terms or []):
        if t and t.lower() not in [s.lower() for s in seen]:
            seen.append(t)
    return " ".join(seen[:12])


def _render_decision(d: Any) -> str:
    """One decision as a prompt line: text + origin marker. Assumes d is
    a memory.decision_store.Decision (duck-typed: .text/.task_id)."""
    try:
        text = str(getattr(d, "text", "") or "").strip()
    except Exception:
        return ""
    if not text:
        return ""
    task = getattr(d, "task_id", None)
    origin = f" (task:{task})" if task else ""
    return f"{text}{origin}"
