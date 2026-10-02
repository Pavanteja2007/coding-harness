# Sessions, checkpoints, and recovery

Neo has several persistence layers. They answer different questions.

| Artifact | Owner and purpose |
|---|---|
| `logs/{task_id}/state.json` | Compacted task progress and the stable state prefix consumed by status, resume, and memory |
| `logs/{task_id}/trace.jsonl` | Authoritative append-only task event history |
| `logs/{task_id}/plan.json` | Harness-internal plan and in-flight attempt bookkeeping |
| `logs/{task_id}.runtime/checkpoint.json` | Runtime scheduler/worker recovery state |
| `logs/{task_id}.runtime/events.jsonl` | Runtime worker events |
| `logs/{task_id}.runtime/model_ledger.jsonl` | Per-call model, token, cost, and routing ledger |
| `logs/_conversations/` | Versioned conversation snapshots and event journals |
| `logs/.neo-sessions.jsonl` | Session index and resumability hints |
| `logs/{task_id}.runtime/` or kernel `checkpoint.json` | Strict-kernel checkpoint, depending on entry point |

A session id, task id, run id, and checkpoint are not interchangeable. The UI may use a session to find a task, while the harness uses the task state to decide whether a fix can continue.

## Resume a verified task

After an interruption:

```bash
neo --list-sessions
neo --resume TASK_ID
# or
neo --continue
```

Inside an interactive session:

```text
/resume
/resume TASK_ID
```

A task is resumable only when its durable state contains completed progress and remaining work and no terminal result. A kill before the first resumable step is intentionally treated as a fresh start by the compatibility contract.

Resume reuses the persisted plan, skips completed steps, keeps the surviving work copy, appends to the existing trace, and continues the in-flight attempt when the state is valid. A fresh non-resume run archives the old task directory rather than deleting it.

## Agent resume

The daily agent can resume under the same task id and replay prior history as structured context. This is history replay, not a claim that the model's old private reasoning is available. Diff, undo, and redo receipts remain tied to the same task.

## Cancellation versus a hard kill

- `/cancel` or Ctrl+C is a cooperative stop. It preserves checkpoints and is the preferred way to pause.
- A scheduler hard kill can happen without a final state write. The next process must validate the checkpoint, repository identity, and workspace before continuing.
- A timeout is a distinct terminal status. It is not automatically a failed fix and not automatically a verified success.

## Recovery rules

1. Inspect `state.json` and the last terminal trace event before choosing a resume command.
2. Use `python -m shared.traceview TASK_ID --logs-root logs --summary` to see the merged lifecycle.
3. If a session snapshot is corrupt, use the explicit quarantine/recovery path; do not hand-edit a journal and then claim continuity.
4. If a workspace edit is stale, preserve the user's change and re-run the edit from current content.
5. If a checkpoint is missing or identity does not match, start a new task id rather than forcing a resume.
6. Never delete the original repository to make a run look clean.

## Export and sharing

`/export` writes a redacted local session artifact. `/share` writes a metadata-oriented shareable view. Neither command is a promise that raw prompts, paths, or repository content are absent; review the artifact before sending it.

## Resume identity

Runtime and strict-kernel checkpoints are bound to a versioned canonical
repository identity, an exact-request SHA-256, a repository revision identity,
and the owning run namespace. Legacy or mismatched checkpoints are not trusted:
runtime workers start a fresh attempt and strict strategies return a blocked
result asking for a new run id. The explicit `continue` request is a
compatibility alias for an existing checkpoint; arbitrary request changes are
rejected.

A model finish without verifier evidence is always `completed_unverified`. A resume cannot upgrade an unverified result by replaying text; it must obtain the declared evidence again.
