"""Reconstruct one task's full lifecycle from ONE place (Task A, part 2).

Merges, chronologically:
  1. the unified cross-module stream  (logs/_trace/{task_id}.jsonl —
     shared.tracing; scheduler/worker/router/sandbox/memory events)
  2. the harness's own trace.jsonl    (T1's authoritative loop record —
     task_start/plan/tool_call/verify/...; READ-ONLY here, never
     rewritten)
  3. the runtime worker journal       (logs/{task_id}.runtime/events.jsonl)
  4. the model-routing ledger         (logs/{task_id}.runtime/model_ledger.jsonl)

so `python -m shared.traceview <task_id>` answers "what did this task do,
end to end" without hunting files. Each merged record is normalized to
{ts, module, event, source, task_id, data}; the original records are
never modified on disk.

CLI:
  python -m shared.traceview <task_id> [--logs-root DIR] [--json]
      --json        print the merged chronology as JSONL
      --logs-root   where logs/ lives (default ./logs; also honors
                    VEX_TRACE_DIR for the unified stream)

API:
  reconstruct_task(task_id, logs_root) -> list[dict]   chronological merge
  render_timeline(events) -> str                       human table
  summarize(events) -> dict                            phase counters
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared import tracing  # noqa: E402 — path boot first

# ---------------------------------------------------------------------------
# Source adapters — each yields normalized records
# ---------------------------------------------------------------------------


def _norm(
    ts: float, module: str, event: str, source: str, task_id: str, data: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "ts": round(float(ts), 3),
        "module": module,
        "event": event,
        "source": source,
        "task_id": task_id,
        "data": data,
    }


def _unified_events(
    task_id: str, logs_root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """The shared.tracing stream for this task (already normalized).

    The stream root is $VEX_TRACE_DIR when set; otherwise falls back to
    <logs_root>/_trace/ (the tracing module's default layout) so a
    reconstruction works without the operator exporting anything —
    traceview is a post-hoc tool, the process that RAN the task set
    the env, not this one.
    """
    if not tracing.enabled():
        # probe the two documented layouts: the module default
        # (<logs_root>/_trace) and the eval-harness per-task dir
        # (<logs_root>/<task_id>/_trace — arms pass <run>/<arm>/<slug>
        # as log_root and the runner isolates each task's stream there)
        for cand in (logs_root, logs_root / task_id):
            p = cand / "_trace"
            if p.is_dir():
                tracing._set_fallback_dir(cand)
                break
    out = []
    for e in tracing.read_task_events(task_id):
        data = {
            k: v
            for k, v in e.items()
            if k not in ("ts", "module", "event", "task_id", "run_id")
        }
        out.append(
            _norm(
                e.get("ts", 0),
                e.get("module", "?"),
                e.get("event", "?"),
                "unified",
                task_id,
                data,
            )
        )
    return out


def _harness_events(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    """The harness's own trace.jsonl (kind-keyed, epoch ts) — read-only.

    Only the KEY events a lifecycle reconstruction needs (full prompts
    and tool outputs stay in the file; the timeline would drown in
    them otherwise — model_request messages are dropped, a pointer
    event keeps their position visible).

    Two layouts are probed: the default `logs_root/{task_id}/trace.jsonl`
    and the eval harness's `logs_root/{task_id}/{task_id}/trace.jsonl`
    (eval arms pass `<run>/<arm>/<slug>` as log_root and core nests the
    task dir under it). First hit wins; both reads are containment-safe
    (safe task ids only, per the reconstruct_task contract).
    """
    candidates = [
        logs_root / task_id / "trace.jsonl",
        logs_root / task_id / task_id / "trace.jsonl",
    ]
    trace_path = next((p for p in candidates if p.is_file()), candidates[0])
    keep = {
        "task_start",
        "task_end",
        "baseline_verify",
        "retrieval",
        "decision_memory",
        "plan",
        "plan_reused",
        "plan_parse_error",
        "attempt_start",
        "attempt_end",
        "attempt_resume",
        "resume",
        "resume_aborted",
        "step_start",
        "step_end",
        "step_skipped_resume",
        "tool_call",
        "verify",
        "final_verify",
        "recall",
        "docs_lookup",
        "lint_failed",
        "final_edit_validation_failed",
        "git_output",
        "rationale",
        "self_critique",
        "self_critique_reject",
        "result",
        "stop",
        "state_transition",
        "context_budget",
    }
    out: List[Dict[str, Any]] = []
    try:
        with open(trace_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = str(ev.get("kind", ""))
                if kind not in keep and not kind.startswith("model_"):
                    continue
                data = dict(ev.get("data") or {})
                if kind.startswith("model_"):
                    # position marker — prompts stay in the file; the
                    # usage block rides along so cost accounting works
                    # for harness-only runs (no router ledger: the
                    # scripted-model evals and in-process `vex fix`)
                    data = {
                        "step": data.get("step"),
                        "usage": data.get("usage"),
                        "note": "full prompt/response in trace.jsonl",
                    }
                out.append(
                    _norm(
                        ev.get("ts", 0), "harness", kind, "trace.jsonl", task_id, data
                    )
                )
    except OSError:
        return out
    return out


def _worker_events(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    """The runtime worker journal (event-keyed, ISO ts)."""
    path = logs_root / f"{task_id}.runtime" / "events.jsonl"
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                out.append(
                    _norm(
                        _iso_to_epoch(ev.get("ts")),
                        "runtime",
                        str(ev.get("event", "?")),
                        "worker-events",
                        task_id,
                        dict(ev.get("data") or {}),
                    )
                )
    except OSError:
        return out
    return out


def _ledger_events(task_id: str, logs_root: Path) -> List[Dict[str, Any]]:
    """Model-routing ledger rows -> one 'route' event per call."""
    from datetime import datetime

    path = logs_root / f"{task_id}.runtime" / "model_ledger.jsonl"
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ts = row.get("ts")
                epoch = None
                try:
                    epoch = datetime.fromisoformat(str(ts)).timestamp()
                except (ValueError, TypeError):
                    epoch = None
                out.append(
                    _norm(
                        epoch or 0,
                        "runtime",
                        "model_routed",
                        "model_ledger",
                        task_id,
                        {
                            "model": row.get("model"),
                            "provider": row.get("provider"),
                            "tokens": row.get("tokens"),
                            "cost_usd": row.get("cost_usd"),
                            "elapsed_s": row.get("elapsed_s"),
                            "routed_via_hint": row.get("routed_via_hint"),
                            "difficulty_hint": row.get("difficulty_hint"),
                        },
                    )
                )
    except OSError:
        return out
    return out


def _iso_to_epoch(iso: Any) -> float:
    """ISO-8601 (worker journal format) -> epoch seconds; 0 on garbage."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Merge + render
# ---------------------------------------------------------------------------


def reconstruct_task(
    task_id: str, logs_root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """One task's full lifecycle, chronologically, from one call.

    Merges the unified cross-module stream, the harness trace, the
    worker journal, and the routing ledger (whichever exist). Assumes
    task_id is a single safe path segment (callers came from a CLI arg
    or internal code; a traversal-shaped id simply matches nothing —
    every source read is containment-safe by construction). Records that
    predate epoch-0 sources are kept in file order after sorting.
    """
    root = Path(logs_root) if logs_root else _default_logs_root()
    events: List[Dict[str, Any]] = []
    events.extend(_unified_events(task_id, root))
    events.extend(_harness_events(task_id, root))
    events.extend(_worker_events(task_id, root))
    events.extend(_ledger_events(task_id, root))
    # stable sort keeps same-second file order deterministic
    events.sort(key=lambda e: e.get("ts", 0))
    return events


def _default_logs_root() -> Path:
    env = ""
    try:
        import os

        env = (os.environ.get("HARNESS_LOGS_DIR") or "").strip()
    except Exception:  # pragma: no cover
        env = ""
    return Path(env) if env else Path.cwd() / "logs"


_PHASE_OF_EVENT = {
    # harness loop phases (order-preserving markers)
    "task_start": "setup",
    "baseline_verify": "verify",
    "retrieval": "retrieval",
    "decision_memory": "memory",
    "plan": "plan",
    "plan_reused": "plan",
    "attempt_start": "attempt",
    "step_start": "step",
    "tool_call": "tool",
    "verify": "verify",
    "final_verify": "verify",
    "attempt_end": "attempt",
    "task_end": "done",
    "result": "done",
}


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact lifecycle summary from a reconstructed timeline.

    Counts by module + event, model calls routed, and the final task
    outcome (the last task_end/result event wins). Assumes events is a
    reconstruct_task() output (chronological).
    """
    by_module: Dict[str, int] = {}
    by_event: Dict[str, int] = {}
    outcome = None
    attempts = 0
    model_calls = 0
    cost_usd = 0.0
    for e in events:
        by_module[e.get("module", "?")] = by_module.get(e.get("module", "?"), 0) + 1
        name = str(e.get("event", "?"))
        by_event[name] = by_event.get(name, 0) + 1
        data = e.get("data") or {}
        if name in ("task_end", "result") and data.get("status"):
            outcome = data["status"]
        if name == "attempt_start":
            attempts = max(attempts, int(data.get("attempt") or attempts or 0))
        if name == "model_routed":
            model_calls += 1
            try:
                cost_usd += float(data.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
        if name == "model_response" and isinstance(data.get("usage"), dict):
            try:
                cost_usd += float(data["usage"].get("cost") or 0)
            except (TypeError, ValueError):
                pass
    return {
        "outcome": outcome,
        "attempts": attempts,
        "model_calls": model_calls,
        "cost_usd": round(cost_usd, 6),
        "by_module": by_module,
        "by_event": by_event,
        "n_events": len(events),
    }


def render_timeline(events: List[Dict[str, Any]]) -> str:
    """Human-readable one-line-per-event table (the CLI default view)."""
    lines = [
        f"{'ts':>12}  {'module':<9} {'event':<24} detail",
        "-" * 100,
    ]
    t0 = events[0]["ts"] if events else 0.0
    for e in events:
        rel = e.get("ts", 0) - t0
        data = e.get("data") or {}
        detail = _fmt_detail(e.get("event", ""), data)
        lines.append(
            f"{rel:>10.1f}s  {e.get('module', '?')!s:<9} "
            f"{e.get('event', '?')!s:<24} {detail}"
        )
    if not events:
        lines.append("(no events found)")
    return "\n".join(lines)


def _fmt_detail(event: str, data: Dict[str, Any]) -> str:
    """One short detail line per event kind (compact, not a dump)."""
    if event == "tool_call":
        return (data.get("command") or "")[:70]
    if event in ("task_start",):
        issue = str(data.get("issue_text") or "")[:60]
        return f"issue: {issue!r}"
    if event in ("task_end", "result"):
        return f"status={data.get('status')}"
    if event in ("verify", "final_verify", "baseline_verify"):
        return (
            f"target={data.get('target_passed', data.get('target_passed_on_pristine'))} "
            f"regression={data.get('regression_passed')} flaky={data.get('flaky')}"
        )
    if event == "model_routed":
        return (
            f"{data.get('model')} via={data.get('routed_via_hint')} "
            f"hint={data.get('difficulty_hint')} ${data.get('cost_usd')}"
        )
    if event == "plan":
        steps = data.get("plan") or []
        return f"{len(steps)} step(s)" if isinstance(steps, list) else ""
    if event == "spawn":
        return f"attempt {data.get('attempt')} pid={data.get('pid')}"
    if event == "state_transition":
        return f"{data.get('from')} -> {data.get('to')}: {data.get('reason')}"
    keep = []
    for k in (
        "step_id",
        "attempt",
        "status",
        "reason",
        "ok",
        "query",
        "matched",
        "strategy",
        "verdict",
        "branch",
        "commit_sha",
    ):
        if k in data and data[k] is not None:
            keep.append(f"{k}={str(data[k])[:40]}")
    return " ".join(keep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.traceview",
        description="Reconstruct one task's full lifecycle from one place.",
    )
    parser.add_argument("task_id", help="the task id to reconstruct")
    parser.add_argument(
        "--logs-root",
        default=None,
        help="logs root (default ./logs or $HARNESS_LOGS_DIR)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print merged chronology as JSONL"
    )
    parser.add_argument(
        "--summary", action="store_true", help="print only the lifecycle summary"
    )
    args = parser.parse_args(argv)

    root = Path(args.logs_root) if args.logs_root else None
    events = reconstruct_task(args.task_id, logs_root=root)
    if args.json:
        for e in events:
            print(json.dumps(e, ensure_ascii=False))
        return 0
    if args.summary:
        print(json.dumps(summarize(events), indent=2, ensure_ascii=False))
        return 0
    print(render_timeline(events))
    print()
    print("summary: " + json.dumps(summarize(events), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
