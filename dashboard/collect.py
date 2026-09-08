"""Scan existing structured logs and build dashboard-ready summaries.

Reads ONLY (never writes): per task dir ``{logs}/{task_id}/`` ->
``state.json`` + ``trace.jsonl`` (Boundary 4, Terminal 1) and the sibling
``{logs}/{task_id}.runtime/`` -> ``model_ledger.jsonl`` +
``checkpoint.json`` (Terminal 3's bookkeeping, layout documented in its
Change Log entry). Works at ANY nesting depth (rglob) so ablation /
stress / benchmark drivers that stage task logs under
``logs/<driver>/<run>/tasklogs/...`` are picked up the same way
memory.decision_store.poll() finds them.

Public surface (used by the CLI command + the HTTP layer):
    scan_logs(logs_dir) -> list[dict]  one summary per task dir
    group_by_run(tasks) -> dict        tasks bucketed by run name

Each task summary dict:
    {task_id, run, rel_dir, status, attempts, cost_usd, model_calls,
     models: {model: calls}, hints: {hint: count}, plan_steps,
     completed_steps, files_touched, decisions, started_ts, ended_ts,
     elapsed_s, issue}

Assumes the documented log formats; anything missing/malformed degrades
to sensible defaults (status "?" / cost 0.0) rather than raising — a
dashboard must never crash on a half-written file.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Statuses a trace "result" event can carry (shared.types.TaskResult).
_KNOWN_STATUSES = ("success", "failed", "error", "timeout")


def _load_json(path: Path) -> Optional[Any]:
    """Parse a JSON file, or None when missing/broken (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail_trace_event(trace_file: Path, *kinds: str) -> Optional[Dict[str, Any]]:
    """Last event of one of `kinds` from a trace.jsonl, scanned from the
    end (traces grow large; full reads would be wasteful)."""
    try:
        text = trace_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("kind") in kinds:
            return obj
    return None


def _first_trace_event(trace_file: Path, kind: str) -> Optional[Dict[str, Any]]:
    """First event of `kind` from a trace.jsonl (task_start holds the
    issue text + config)."""
    try:
        with trace_file.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("kind") == kind:
                    return obj
    except OSError:
        return None
    return None


def _summarize_task_dir(task_dir: Path, logs_dir: Path) -> Optional[Dict[str, Any]]:
    """Build one task summary from a task dir holding a state.json.

    Assumes task_dir is named after the task and may have a sibling
    ``<name>.runtime/`` dir with the runtime bookkeeping. Returns None
    when there's no state.json (not a task dir).
    """
    state = _load_json(task_dir / "state.json")
    if not isinstance(state, dict):
        return None
    task_id = str(state.get("task_id") or task_dir.name)

    summary: Dict[str, Any] = {
        "task_id": task_id,
        "run": _run_name(task_dir, logs_dir),
        "rel_dir": task_dir.relative_to(logs_dir).as_posix(),
        "status": "?",
        "attempts": 0,
        "cost_usd": 0.0,
        "model_calls": 0,
        "models": {},
        "hints": {},
        "plan_steps": len(state.get("plan") or []),
        "completed_steps": len(state.get("completed_steps") or []),
        "files_touched": list(state.get("files_touched") or []),
        "decisions": list(state.get("decisions") or []),
        "started_ts": None,
        "ended_ts": None,
        "elapsed_s": None,
        "issue": "",
    }

    # -- trace.jsonl: result status/cost + task_start issue/config -------
    trace_file = task_dir / "trace.jsonl"
    if trace_file.is_file():
        result = _tail_trace_event(trace_file, "result", "task_end")
        if result:
            data = result.get("data") or {}
            status = data.get("status")
            if status in _KNOWN_STATUSES:
                summary["status"] = status
            if isinstance(data.get("attempts"), int):
                summary["attempts"] = data["attempts"]
            try:
                summary["cost_usd"] = max(0.0, float(data.get("cost_usd", 0.0)))
            except (TypeError, ValueError):
                pass
        start = _first_trace_event(trace_file, "task_start")
        if start:
            data = start.get("data") or {}
            summary["issue"] = str(data.get("issue_text") or "")[:200]
            summary["started_ts"] = data.get("ts") or start.get("ts")
        if isinstance(summary.get("started_ts"), (int, float)):
            ended = result.get("ts") if result else None
            if isinstance(ended, (int, float)) and ended >= summary["started_ts"]:
                summary["ended_ts"] = ended
                summary["elapsed_s"] = round(ended - summary["started_ts"], 1)

    # -- sibling .runtime/ dir: model ledger + checkpoint (fallback) -----
    runtime_dir = task_dir.with_name(task_dir.name + ".runtime")
    ledger = runtime_dir / "model_ledger.jsonl"
    if ledger.is_file():
        models: Dict[str, int] = {}
        hints: Dict[str, int] = {}
        calls = 0
        cost = 0.0
        try:
            with ledger.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    calls += 1
                    model = str(row.get("model") or "?")
                    models[model] = models.get(model, 0) + 1
                    hint = str(row.get("difficulty_hint") or "?")
                    hints[hint] = hints.get(hint, 0) + 1
                    try:
                        cost += float(row.get("cost_usd", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        pass
        except OSError:
            pass
        summary["model_calls"] = calls
        summary["models"] = models
        summary["hints"] = hints
        if cost > 0 and summary["cost_usd"] <= 0:
            summary["cost_usd"] = round(cost, 6)
        if calls and not summary["attempts"]:
            checkpoint = _load_json(runtime_dir / "checkpoint.json")
            if isinstance(checkpoint, dict) and isinstance(
                checkpoint.get("attempt"), int
            ):
                summary["attempts"] = checkpoint["attempt"] + 1

    # -- runtime checkpoint alone (no trace: crashed before task_start) --
    if summary["status"] == "?" and runtime_dir.is_dir():
        checkpoint = _load_json(runtime_dir / "checkpoint.json")
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("result"), dict):
            status = checkpoint["result"].get("status")
            if status in _KNOWN_STATUSES:
                summary["status"] = status
            try:
                summary["cost_usd"] = max(
                    0.0, float(checkpoint["result"].get("cost_usd", 0.0))
                )
            except (TypeError, ValueError):
                pass

    return summary


def _run_name(task_dir: Path, logs_dir: Path) -> str:
    """Bucket name for grouping: the path segment chain between the logs
    root and the task dir, minus 'tasklogs' (drivers stage tasks under
    <root>/<driver>/<run>/tasklogs/<task>). Top-level tasks -> 'adhoc'."""
    parts = task_dir.relative_to(logs_dir).parts[:-1]  # drop task dir itself
    parts = [p for p in parts if p not in ("tasklogs",)]
    if not parts:
        return "adhoc"
    return "/".join(parts)


def scan_logs(logs_dir: str) -> List[Dict[str, Any]]:
    """Summarize every task under logs_dir (any depth).

    Assumes logs_dir follows the documented layout. Returns one summary
    per directory holding a state.json, newest run activity first.
    Never raises; unreadable entries are skipped.
    """
    root = Path(logs_dir)
    if not root.is_dir():
        return []
    tasks: List[Dict[str, Any]] = []
    for state_file in root.rglob("state.json"):
        summary = _summarize_task_dir(state_file.parent, root)
        if summary is not None:
            tasks.append(summary)
    tasks.sort(key=lambda t: (str(t.get("started_ts") or 0), t["rel_dir"]), reverse=True)
    return tasks


def group_by_run(tasks: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket task summaries by run; runs sorted newest-first, tasks in
    scan order within a run."""
    runs: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        runs.setdefault(t["run"], []).append(t)
    return dict(sorted(runs.items(), key=lambda kv: min(
        str(t.get("started_ts") or 0) for t in kv[1] or [{}]), reverse=True))


def aggregate(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pass/fail/cost rollups for a set of task summaries."""
    counts = {"success": 0, "failed": 0, "error": 0, "timeout": 0, "?": 0}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    total_cost = sum(t.get("cost_usd", 0.0) for t in tasks)
    return {
        "total": len(tasks),
        "counts": counts,
        "cost_usd": round(total_cost, 6),
        "model_calls": sum(t.get("model_calls", 0) for t in tasks),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
