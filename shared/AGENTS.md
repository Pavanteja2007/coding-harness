# AGENTS.md — shared/: cross-module structured tracing (Task A)

(Observability round. shared/ is the BOTTOM dependency layer — every
module may import it, it imports none of them. It already held
shared/types.py; this round added the tracing layer.)

## What this module is

One append-only, normalized event stream per task that EVERY module
emits into — so a task's full lifecycle (planning, tool calls,
verification, routing decisions, memory queries, sandbox commands) is
reconstructible from ONE place instead of being pieced together from
half a dozen files in three trees.

## Where the output lives + how to read it

- **Stream root**: `$VEX_TRACE_DIR` (a directory). Unset → tracing is a
  no-op (zero overhead; tests stay quiet by default).
- **Per-task stream**: `<VEX_TRACE_DIR>/_trace/<task_id>.jsonl` —
  records `{"ts": <epoch>, "module", "event", "task_id", ...}`.
- **Run-scoped events** (scheduler journal overlay):
  `<VEX_TRACE_DIR>/_trace/_run-<run_id>.jsonl`.
- **Who sets the env**: entry points do — `vex fix` defaults it to the
  logs root; `evals.run` defaults it to the eval logs root and
  re-points it per task (arm isolation). Worker/scheduler subprocesses
  inherit it from their spawner. A manual run:
  `$env:VEX_TRACE_DIR = "logs"` then run anything.

**Read it back** (merged with the harness's own trace.jsonl, the
worker journal, and the routing ledger — whichever exist — into one
chronological timeline):

```powershell
python -m shared.traceview <task_id> [--logs-root DIR] [--json] [--summary]
```

`--logs-root` defaults to ./logs (or $HARNESS_LOGS_DIR); the unified
stream is found via $VEX_TRACE_DIR or the `<logs_root>/_trace/` /
`<logs_root>/<task_id>/_trace/` layout probes (the eval harness's
per-task isolation). The harness trace adapter also probes the eval
layout `<logs_root>/<task_id>/<task_id>/trace.jsonl`. model_request/
response events are position markers (prompts stay in trace.jsonl) but
carry the usage block so cost accounting works without a router ledger.

## The contracts (every emitter must hold these)

1. **NEVER RAISE** — tracing is observability, not correctness. `emit()`
   swallows everything; a tracing failure must never change a task's
   outcome.
2. **OPT-IN via env** — `VEX_TRACE_DIR` unset = no-op. The env is read
   ONCE per process and cached (tests use `_reset_cache()`;
   traceview's reader uses `_set_fallback_dir()` for post-hoc reads
   where the env isn't exported).
3. **ONE FILE PER TASK** — the harness's own `logs/{task_id}/trace.jsonl`
   stays THE authoritative full record; this stream is the cross-module
   OVERLAY (compact — no full prompts; depth lives where it lives).
4. **ts = time.time() epoch, 3 decimals** — matches harness trace.jsonl
   so merged views sort consistently.
5. **safe_segment** — one shared Win32-safe single-segment gate
   (separators, `:`-drives, null bytes, edge whitespace, trailing
   dot/space aliases). Implemented locally (shared imports nothing).

## Who emits what (landed)

| layer | events |
|---|---|
| runtime/scheduler | task_spawn, task_finish, run bookkeeping (both the run file and per-task streams) |
| runtime/worker | worker lifecycle markers (start, harness call boundaries, approval gate, finish) |
| runtime/model_router | `model_routed` per call (model, tier, hint, cost) — task_id from the router context |
| execution/sandbox | `sandbox_call` / `sandbox_result` per containerized command — task id derived from the mounted repo path (`logs/{task_id}/work`) |
| mcp_server | `memory_query` (per query; also a run-scoped journal) |
| harness | its own trace.jsonl remains the authority; traceview merges it in (no duplicate emission) |

## Bugs found while validating (fixed + test-pinned)

- **Win32 path rejection disabled ALL execution-layer tracing**:
  `_trace_task_id` in execution/sandbox.py rejected any raw path
  containing a backslash (a traversal defense) — on a Windows host
  every repo path is native-form, so sandbox events NEVER emitted.
  Fix: normalize separators for structure parsing, keep the `..`/`.`
  component rejection, single-segment safety via safe_segment on the
  extracted name. The old blanket rejection is now
  `test_task_id_from_work_dir`'s backslash-form assertions (native
  paths MUST trace).

## Files

| File | Role |
|---|---|
| `tracing.py` | emit/emit_run/readers; safe_segment; env caching + test hooks |
| `traceview.py` | reconstruct_task + render_timeline + summarize + the `python -m shared.traceview` CLI |

## Tests

`tests/test_tracing.py` (38, Docker-less): record shape, no-op-off,
never-raise, reserved keys, run files, traversal-shaped ids (both
writer and readers), corrupt-line tolerance, scheduler dual-stream,
router context usage, the sandbox path adapter (incl. the Win32 fix),
cross-source chronological merge, summary counters, CLI modes.

## Deliberate scope

- Lightweight custom format, NOT OpenTelemetry (per the round brief —
  the schema is JSONL one-record-per-line; an OTLP exporter could be
  layered later behind the same emit() seam if ever needed).
- No tracing of full prompts/model bodies in the unified stream (size +
  privacy of the overlay; the authoritative depth is trace.jsonl, which
  traceview merges).
