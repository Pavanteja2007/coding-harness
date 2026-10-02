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
                    NEO_TRACE_DIR for the unified stream)
      --privacy     local_only | redacted | shareable derived view
      --spans       print the reconstructed GenAI span lifecycle
      --lifecycle   print the span-coverage/integrity verdict only
      --otlp        print the OTLP/JSON projection of the spans

API:
  reconstruct_task(task_id, logs_root, privacy=...) -> list[dict]
  reconstruct_spans(task_id, logs_root, privacy=...) -> list[dict]
  span_lifecycle(spans) -> dict
  render_timeline(events) -> str                       human table
  summarize(events) -> dict                            phase counters
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared import tracing  # noqa: E402 — path boot first
from shared.privacy import privacy_mode, privacy_view  # noqa: E402
from shared.security import redact_secrets, safe_path  # noqa: E402

# ---------------------------------------------------------------------------
# Source adapters — each yields normalized records
# ---------------------------------------------------------------------------


def _norm(
    ts: float, module: str, event: str, source: str, task_id: str, data: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "ts": round(float(ts), 3),
        "module": redact_secrets(str(module)),
        "event": redact_secrets(str(event)),
        "source": redact_secrets(str(source)),
        "task_id": redact_secrets(str(task_id)),
        "data": redact_secrets(dict(data or {})),
    }


def _unified_events(
    task_id: str, logs_root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """The shared.tracing stream for this task (already normalized).

    The stream root is $NEO_TRACE_DIR when set; otherwise falls back to
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
        Path(task_id) / "trace.jsonl",
        Path(task_id) / task_id / "trace.jsonl",
    ]
    trace_path = next(
        (
            safe_path(logs_root, relative, must_exist=True)
            for relative in candidates
            if safe_path(logs_root, relative, must_exist=True) is not None
        ),
        safe_path(logs_root, candidates[0]),
    )
    if trace_path is None:
        return []
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
        "tool_result",
        "verification",
        "run_started",
        "run_finished",
        "strategy_selected",
        "tool_started",
        "tool_completed",
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
        "context_compacted",
        "context_compaction_skipped",
        "context_compaction_fallback_failed",
        "context_compaction_fallback_unavailable",
        "context_compaction_restored",
        "context_rewind",
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
                kind = str(ev.get("event") or ev.get("kind") or "")
                raw_data = ev.get("payload")
                if not isinstance(raw_data, dict):
                    raw_data = ev.get("data")
                data = dict(raw_data or {})
                if kind == "verification":
                    kind = "verify"
                if kind not in keep and not kind.startswith("model_"):
                    continue
                if kind.startswith("model_"):
                    # position marker — prompts stay in the file; the
                    # usage block rides along so cost accounting works
                    # for harness-only runs (no router ledger: the
                    # scripted-model evals and in-process `neo fix`)
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
    path = safe_path(
        logs_root, Path(f"{task_id}.runtime") / "events.jsonl", must_exist=True
    )
    if path is None:
        return []
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

    path = safe_path(
        logs_root, Path(f"{task_id}.runtime") / "model_ledger.jsonl", must_exist=True
    )
    if path is None:
        return []
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
    task_id: str,
    logs_root: Optional[Path] = None,
    *,
    privacy: str = "local_only",
) -> List[Dict[str, Any]]:
    """One task's full lifecycle, chronologically, from one call.

    Merges the unified cross-module stream, the harness trace, the worker
    journal, and the routing ledger without modifying any source file. The
    optional privacy mode controls the derived view; ``local_only`` preserves
    useful local content while redacting credentials.
    """
    root = Path(logs_root) if logs_root else _default_logs_root()
    events: List[Dict[str, Any]] = []
    events.extend(_unified_events(task_id, root))
    events.extend(_harness_events(task_id, root))
    events.extend(_worker_events(task_id, root))
    events.extend(_ledger_events(task_id, root))
    events.sort(key=lambda e: e.get("ts", 0))
    return privacy_view(events, privacy_mode(privacy))


def _default_logs_root() -> Path:
    env = ""
    try:
        import os

        env = (os.environ.get("HARNESS_LOGS_DIR") or "").strip()
    except Exception:  # pragma: no cover
        env = ""
    return Path(env) if env else Path.cwd() / "logs"


# ---------------------------------------------------------------------------
# Span lifecycle — explicit GenAI spans + derived spans for older traces
# ---------------------------------------------------------------------------

#: Harness/Runtime trace events that carry a measurable span even when the
#: producer has not (yet) emitted an explicit ``genai_*`` pair. Each entry
#: maps the event name to its GenAI kind so a run recorded before the span
#: API still reconstructs. Derived spans are always labelled as such.
_DERIVED_SPAN_KINDS: Dict[str, str] = {
    "run_started": "model",
    "run_finished": "model",
    "model_request": "model",
    "model_response": "model",
    "model_routed": "routing",
    "tool_call": "tool",
    "tool_started": "tool",
    "tool_completed": "tool",
    "tool_result": "tool",
    "sandbox_call": "tool",
    "sandbox_result": "tool",
    "retrieval": "retrieval",
    "recall": "retrieval",
    "docs_lookup": "retrieval",
    "verify": "verify",
    "baseline_verify": "verify",
    "final_verify": "verify",
    "verification": "verify",
    "cost_ledger": "cost",
}

#: Attribute names every reconstructed span must carry for a run to count as
#: correlated. A span without them is still returned, but flagged.
_CORRELATION_FIELDS = ("trace_id", "task_id", "session_id", "model")


def _derived_span(
    event: Dict[str, Any], source: str, trace_id: str
) -> Optional[Dict[str, Any]]:
    """One legacy trace event -> one labelled derived span, or None."""
    name = str(event.get("event") or "")
    kind = _DERIVED_SPAN_KINDS.get(name)
    if kind is None:
        return None
    data = event.get("data") or {}
    seed = f"{source}:{name}:{event.get('ts')}:{data.get('step')}"
    return {
        "span_id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
        "trace_id": trace_id,
        "parent_span_id": "",
        "kind": kind,
        "name": name,
        "task_id": str(event.get("task_id") or ""),
        "run_id": str(data.get("run_id") or ""),
        "session_id": str(data.get("session_id") or ""),
        "model": str(data.get("model") or ""),
        "start_ts": event.get("ts"),
        "end_ts": event.get("ts"),
        "duration_ms": None,
        "status": "ok",
        "open": False,
        "attributes": data if isinstance(data, dict) else {},
        "origin": "derived",
        "source": source,
    }


def reconstruct_spans(
    task_id: str,
    logs_root: Optional[Path] = None,
    *,
    privacy: str = "local_only",
    include_derived: bool = True,
) -> List[Dict[str, Any]]:
    """Reconstruct one run's complete GenAI span lifecycle.

    Explicit ``genai_*`` start/end pairs come from the unified stream;
    when ``include_derived`` is set, legacy harness/runtime events without
    an explicit span are also projected so an older run still reconstructs
    (each such row carries ``origin="derived"``). The result is one flat,
    chronological span list; :func:`span_lifecycle` scores its coverage.
    """
    root = Path(logs_root) if logs_root else _default_logs_root()
    events = reconstruct_task(task_id, logs_root=root, privacy="local_only")
    explicit = tracing.read_genai_spans(task_id)
    explicit_ids = {str(span.get("span_id")) for span in explicit}
    trace_id = tracing.trace_id_for(task_id)
    derived: List[Dict[str, Any]] = []
    if include_derived:
        for event in events:
            span = _derived_span(event, str(event.get("source") or "?"), trace_id)
            if span is None or span["span_id"] in explicit_ids:
                continue
            derived.append(span)
    rows = [
        {**dict(span), "origin": "explicit", "source": "unified"} for span in explicit
    ] + derived
    rows.sort(key=lambda span: (span.get("start_ts") or 0, str(span.get("span_id"))))
    return privacy_view(rows, privacy_mode(privacy))


def span_lifecycle(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Coverage/integrity verdict for a reconstructed span set.

    Reports required-kind coverage, open spans, uncorrelated spans, and
    the count of legacy-derived rows. ``reconstructable`` is true only
    when every required kind is present, nothing is open, at least one
    explicit span exists, and every span carries the correlation fields.
    """
    from shared.tracing import span_lifecycle as _lifecycle

    verdict = _lifecycle(spans)
    derived = sum(1 for span in spans if span.get("origin") == "derived")
    explicit = len(spans) - derived
    uncorrelated = [
        span.get("span_id")
        for span in spans
        if any(not span.get(field) for field in _CORRELATION_FIELDS)
    ]
    verdict.update(
        {
            "explicit_span_count": explicit,
            "derived_span_count": derived,
            "uncorrelated_span_count": len(uncorrelated),
            "uncorrelated_span_ids": uncorrelated,
            "correlation_fields": list(_CORRELATION_FIELDS),
            "reconstructable": bool(
                verdict.get("reconstructable") and explicit > 0 and not uncorrelated
            ),
        }
    )
    return verdict


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
    "context_compacted": "context",
    "context_rewind": "context",
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
    terminal_cost: Optional[float] = None
    for e in events:
        by_module[e.get("module", "?")] = by_module.get(e.get("module", "?"), 0) + 1
        name = str(e.get("event", "?"))
        by_event[name] = by_event.get(name, 0) + 1
        data = e.get("data") or {}
        if name in ("task_end", "result", "run_finished"):
            nested = data.get("result")
            result = nested if isinstance(nested, dict) else data
            if result.get("status"):
                outcome = result["status"]
            if "cost_usd" in result or "cost" in result:
                try:
                    terminal_cost = float(
                        result.get("cost_usd") or result.get("cost") or 0
                    )
                except (TypeError, ValueError):
                    terminal_cost = None
        if name == "attempt_start":
            attempts = max(attempts, int(data.get("attempt") or attempts or 0))
        if name == "model_routed":
            model_calls += 1
            try:
                cost_usd += float(data.get("cost_usd") or data.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        if name == "model_response" and isinstance(data.get("usage"), dict):
            try:
                usage = data["usage"]
                cost_usd += float(usage.get("cost") or usage.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
    return {
        "outcome": outcome,
        "attempts": attempts,
        "model_calls": model_calls,
        "cost_usd": round(terminal_cost if terminal_cost is not None else cost_usd, 6),
        "by_module": by_module,
        "by_event": by_event,
        "n_events": len(events),
    }


def _spans_as_events(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project spans onto the render_timeline event shape (read-only view)."""
    return [
        {
            "ts": span.get("start_ts") or 0.0,
            "module": f"genai/{span.get('origin', 'explicit')}",
            "event": f"span_{span.get('kind', 'model')}",
            "task_id": span.get("task_id", ""),
            "data": {
                "span_id": span.get("span_id"),
                "model": span.get("model"),
                "session_id": span.get("session_id"),
                "status": span.get("status"),
                "duration_ms": span.get("duration_ms"),
            },
        }
        for span in spans
    ]


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
    if event == "context_budget":
        return (
            f"{data.get('used')}/{data.get('limit')} tok "
            f"({round(float(data.get('utilization') or 0.0) * 100)}%) "
            f"turn={data.get('turn')} stage={data.get('stage')}"
        )
    if event == "context_compacted":
        survived = (
            data.get("survived") if isinstance(data.get("survived"), dict) else {}
        )
        return (
            f"{data.get('method')} {data.get('before_tokens')}->"
            f"{data.get('after_tokens')} tok, dropped {data.get('dropped_messages')} "
            f"msg, kept {survived.get('retained_messages', '?')} "
            f"(turn {data.get('first_turn')}-{data.get('last_turn')})"
        )
    if event == "context_compaction_skipped":
        return f"reason={data.get('reason')} used={data.get('used')}"
    if event in (
        "context_compaction_fallback_failed",
        "context_compaction_fallback_unavailable",
    ):
        return f"turn={data.get('turn')} reason={data.get('reason') or data.get('primary_error')}"
    if event == "context_compaction_restored":
        return (
            f"{data.get('compaction_id')} restored {data.get('restored_messages')} msg"
        )
    if event == "context_rewind":
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        conversation = (
            data.get("conversation")
            if isinstance(data.get("conversation"), dict)
            else {}
        )
        return (
            f"turn={data.get('turn')} scope={data.get('scope')} "
            f"files={len(files.get('restored') or []) + len(files.get('deleted') or [])} "
            f"conversation_rows={conversation.get('kept_rows')}"
        )
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
    parser.add_argument(
        "--privacy",
        choices=("local_only", "redacted", "shareable"),
        default="local_only",
        help="privacy mode for the derived view (default: local_only)",
    )
    parser.add_argument(
        "--spans",
        action="store_true",
        help="reconstruct the GenAI span lifecycle instead of the event timeline",
    )
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="print the span coverage/integrity verdict as JSON",
    )
    parser.add_argument(
        "--otlp",
        action="store_true",
        help="print the OTLP/JSON projection of the reconstructed spans",
    )
    parser.add_argument(
        "--no-derived-spans",
        action="store_true",
        help="only include explicit genai_* spans, never legacy-derived rows",
    )
    args = parser.parse_args(argv)

    root = Path(args.logs_root) if args.logs_root else None
    if args.spans or args.lifecycle or args.otlp:
        spans = reconstruct_spans(
            args.task_id,
            logs_root=root,
            privacy=args.privacy,
            include_derived=not args.no_derived_spans,
        )
        lifecycle = span_lifecycle(spans)
        if args.otlp:
            from shared.otel import to_otlp

            print(
                json.dumps(
                    to_otlp(spans, privacy=args.privacy),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.lifecycle:
            print(json.dumps(lifecycle, indent=2, ensure_ascii=False, default=str))
            return 0
        print(render_timeline(_spans_as_events(spans)))
        print()
        print("lifecycle: " + json.dumps(lifecycle, ensure_ascii=False, default=str))
        return 0
    events = reconstruct_task(args.task_id, logs_root=root, privacy=args.privacy)
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
