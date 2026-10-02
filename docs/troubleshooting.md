# Troubleshooting and recovery

Start with the exit code and the task report. Do not paste credentials, raw API keys, connector commands, or unredacted trace payloads into an issue.

## Quick diagnosis

| Symptom | Category | First checks | Recovery |
|---|---|---|---|
| Docker daemon not reachable | 3 | `docker info`, task `state.json`, first trace event | Start Docker and rerun; do not force an unsandboxed fallback |
| Auth rejected, endpoint unreachable, rate limited | 4 | `neo config status --json`, provider health, router ledger | Rotate/replace the key, check base URL/model, rerun a small canary |
| Bad flag, TOML, subset, or path | 2 | stderr, `neo config list`, input file encoding | Correct the input; the CLI should not emit a traceback |
| Model says done but no verifier ran | Task result | `status`, `verification`, `trace.jsonl` | Treat as `completed_unverified`; declare a target/test command and rerun if verification is required |
| Permission denied or approval parked | 4/1 depending on task | `approval_required`, `approval_decided`, mode policy | Approve the exact effect or use a read-only mode; reject leaves the task non-success |
| Session or checkpoint is corrupt | Recovery | `state.json`, session corruption receipt, `trace.jsonl` | Use quarantine/recovery or start a new task id; never hand-edit a journal and call it resumed |
| Stale edit conflict | Safety | workspace revision and user-change receipt | Re-read the current file and reissue the edit; do not overwrite the user's change |
| MCP connector fails | Connector | `neo mcp list`, `neo mcp health`, connector source | Fix the launch command or environment, then retry; inspect bounded error output |
| TUI/REPL appears stuck | Environment/UI | `trace.jsonl`, process/container census, `/status` | Use `/cancel`; if the process is hard-killed, resume only after checkpoint validation |
| `neo` exits 2 with no prompt in a pipe | Usage | `sys.stdin.isatty()` and selected command | Use `neo fix`, `neo status`, or another explicit subcommand |
| Offline demo hangs or is slow | Demo | current demo script and its isolated work tree | Use the current `demo/run_demo.py`; it bounds the graph query to the demo repository |

## Docker

```bash
docker info
python -m execution.sandbox --repo . "python -V"
```

The production fix path deliberately raises `SandboxUnavailableError` rather than silently running on the host. A Docker-dependent test or eval lane is blocked, not passed, when the daemon is unavailable. The offline demo's local subprocess stub is explicit and should not be used to diagnose production isolation.

## Provider and router

Check the effective source without exposing secrets:

```bash
neo config status --json
neo mcp health
```

For a remote endpoint, test the smallest possible request before a long benchmark. Check the router ledger for endpoint, model, tier, tokens, and latency. Adaptive routing can escalate after a struggle signal, so a single expensive call is not automatically a configuration error.

If a model is unavailable, do not convert its failure into a fixture pass. Record the provider lane as blocked and retain the deterministic evidence with its label.

## Workspace and dirty trees

Before a run, save a user diff or commit your own work. The harness protects the original tree for verified fixes, but the live agent path intentionally edits the current checkout. Use a dedicated branch or disposable clone for large interactive work. Never use reset/clean/checkout to recover a Neo run; inspect the task artifacts and use the explicit undo/resume surfaces.

## Session recovery

```bash
neo --list-sessions
cat logs/TASK_ID/state.json
python -m shared.traceview TASK_ID --logs-root logs --summary
```

If the state is not resumable, use a new task id. If it is resumable, prefer `neo --resume TASK_ID` over manually copying directories. Same-id cross-repository resume is currently a known identity-hardening blocker; use unique ids and log roots.

## Evidence hygiene

Use these labels in reports:

- `real` for a real provider/Docker/product path;
- `deterministic` for scripted fixtures;
- `blocked` for an unavailable environment;
- `source-only` for a checkout feature not in the wheel.

A passing deterministic receipt, a provider health ping, and a real end-to-end fix are different claims. The [dogfood report](dogfood-report.md) keeps them separate.
