# Architecture and event schema

Neo is a library first, with CLI, runtime, and MCP consumers around the same contracts. The diagram shows the product boundaries; the source modules are intentionally not interchangeable private implementations.

## Four layers

```text
CLI / SDK / MCP client
          |
          v
Runtime scheduler and workers
          |
          v
Harness or AgentKernel
      /             \
Execution          Memory + MCP
Docker/verifier    code graph, decisions, status
```

- **Harness**: retrieval, planning, edit validation, context management, verifier-gated fix, git-native output, and rationale.
- **Execution**: Docker sandbox, stateless verification, flake detection, and deterministic product artifacts.
- **Runtime**: process workers, concurrency, checkpoints, approval gates, provider routing, and per-call ledgers.
- **Memory/MCP**: structural code graph, decision memory, status queries, and stdio MCP server/client.
- **AgentKernel**: the versioned strict session/run path used by explicit modes, the SDK, and the daily product surfaces. The historical `harness.core.run_task` and `harness.agent_loop.run_agent` facades remain compatibility APIs.

## Two execution paths

`harness.core.run_task(Task)` is the stable verified-fix facade. It delegates to a verified-fix `RunSpec`/kernel lifecycle and preserves the historical `TaskResult` and six-key `state.json` prefix.

`harness.agent_loop.run_agent(...)` is the compatibility daily-agent facade. It operates on the live repository and preserves its result shape while routing through the kernel/session compatibility layer.

Explicit `/mode` sessions use `AgentKernel` strategies directly. A model finish without a declared verifier is `completed_unverified`; only clean verification evidence can produce `completed_verified`.

## Run contracts

The canonical contracts are in `shared/agent_contracts.py` and are re-exported by `harness.agent_kernel`:

- `SessionState`: bounded conversation state;
- `RunSpec`: JSON-only identity, strategy, workspace, verification, resume, and metadata;
- `ToolCall` and `PermissionDecision`: typed tool/policy records;
- `RunEvent`: one ordered journal row;
- `Checkpoint`: durable event sequence and resume references;
- `RunResult`: canonical completion status, evidence, cost, diff, and paths.

Every serialized contract has `schema_version = 1`. Unsupported versions fail closed.

The exact completion values are:

```text
completed_verified | completed_unverified | needs_input | blocked |
failed | cancelled | timeout
```

## Trace event schema

The authoritative task journal is append-only `logs/{task_id}/trace.jsonl`. Current rows carry the canonical fields and legacy aliases:

```json
{
  "schema_version": 1,
  "sequence": 12,
  "session_id": "session-1",
  "run_id": "task-1",
  "turn_id": "turn-3",
  "event": "tool_completed",
  "payload": {"tool": "bash", "exit_code": 0},
  "timestamp": 1750000000.123,
  "kind": "tool_completed",
  "data": {"tool": "bash", "exit_code": 0}
}
```

The legacy `kind`/`data` fields remain for older readers. New consumers should prefer `event`/`payload` and treat unknown additive fields as ignorable.

Common lifecycle events include:

```text
run_started, strategy_selected, run_finished
task_start, plan, retrieval, attempt_start, attempt_end
model_request, model_response, tool_call, tool_result
verification, final_verify, run_finished, result
approval_required, approval_decided, checkpoint_saved
cancel_requested, cancelled, timeout, blocked
```

Tool-specific receipts include `skills`, `decision_memory`, `docs_lookup`, `web_fetch`, `recall`, `coordination`, `git_output`, and `rationale`.

## Structured state

`logs/{task_id}/state.json` is the compacted progress authority. The stable six-key prefix remains:

```json
{
  "task_id": "task-1",
  "plan": ["..."],
  "completed_steps": ["..."],
  "files_touched": ["src/module.py"],
  "decisions": ["..."],
  "remaining_plan": ["..."]
}
```

Optional additive keys such as `phase`, `repo_path`, and `change_groups` may follow. Consumers must ignore unknown additive keys.

## Unified tracing and replay

When `NEO_TRACE_DIR` is set, shared tracing writes a compact cross-module overlay at `_trace/<task_id>.jsonl`. The harness `trace.jsonl` remains the full authority; the overlay never replaces it.

```powershell
$env:NEO_TRACE_DIR = "logs"
python -m shared.traceview task-1 --logs-root logs --summary
python -m shared.traceview task-1 --logs-root logs --json
```

`shared.traceview` merges, read-only:

1. the unified overlay;
2. the harness trace;
3. the runtime worker journal;
4. the model ledger.

`harness.agent_kernel.replay_run(path)` validates schema, identity, and contiguous sequence numbers, then produces a deterministic semantic projection without calling a model or executing a tool. A replay is evidence of recorded events, not a fresh verification.

## Privacy and retention

Status, export, share, and trace projections use redaction and containment rules. `trace.jsonl` is the sensitive local record; do not publish it wholesale. Use the privacy-aware views and inspect the resulting artifact before sharing.

## Persistence boundary

The strict kernel, compatibility session facade, and legacy harness paths use
separate persistence authorities, but resume checkpoints now share a versioned
repository/request/revision/namespace identity contract. Mismatched or legacy
checkpoints fail closed; this is not a claim that all persistence formats have
been unified.
