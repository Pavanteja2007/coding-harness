# coding-harness

An AI coding agent that **fixes real software bugs end-to-end** — reads an
issue, understands the codebase, writes a fix, verifies it in a Docker
sandbox, and retries with feedback until the tests (not the model) say it's
done — running on a custom concurrent runtime with a persistent
cross-session memory layer exposed over MCP, and an adaptive model router
as the measured novel mechanism.

Full context, rationale, and scope: [`project-spec.md`](project-spec.md).
How the four modules talk to each other: [`INTERFACES.md`](INTERFACES.md).

## What it does

- **Fixes bugs end-to-end**: `harness fix --repo <path> --issue "<bug report>"`
  plans sub-steps, edits a working copy via bash (the agent never touches your
  original repo), runs tests in a fresh Docker container per command, and only
  reports success when the target test passes AND the full suite shows no
  regression AND the test isn't flaky. Verified with a real cloud model
  (issue → correct minimal diff, $0.0093/task on the fixture suite).
- **Runs bug sets concurrently**: `harness run-benchmark` fans tasks out
  through a scheduler with worker subprocesses, checkpoint/resume (kill an
  agent mid-task; the relaunch continues from the last completed step), crash
  budgets, hang detection, and an optional human-approval gate. Stress-tested
  at 50 concurrent agents with fault injection.
- **Adaptive model routing (the novel mechanism)**: per-call difficulty
  prediction (intrinsic signal from the issue text + struggle signal from the
  conversation) routes easy steps to cheap models and hard ones to expensive
  tiers. Ablated on the 5-bug fixture suite with the real stack: **100%
  success in both arms, adaptive routing at ~45% of the always-expensive
  arm's cost** (`logs/ablations/`, honesty notes included).
- **Persistent memory**: a tree-sitter code graph (functions/classes/
  imports/calls) + a SQLite decision store, both exposed as an **MCP server**
  — any MCP client (Claude Code, Cursor, ...) can query your project's
  structure and accumulated decisions. The harness itself uses the graph for
  structural retrieval (finds the right file even when the issue names none
  of them).

## Quick start

Requirements: Python 3.10+, Docker (for sandboxed verification), and a model
backend (any litellm-supported provider — bring your own key; Ollama works
for local runs).

```bash
pip install -e .          # installs the `harness` CLI + deps (tree-sitter, mcp)
pytest                    # full suite (offline; Docker needed for e2e tests)
```

### CLI

```bash
# Fix one bug in one repo (single-task mode)
harness fix --repo ./path/to/repo --issue "The mean() function returns the sum instead of the mean." \
            --model <name> --provider <litellm-provider> --api-key <key>

# Run a benchmark subset through the concurrent scheduler
harness run-benchmark --subset smoke                 # offline plumbing check
harness run-benchmark --subset tasks.json --concurrency 10   # JSON task list

# Inspect what happened (plan checklist, files, decisions, result, cost)
harness status --task-id <id>

# Read-only web dashboard over existing logs (auto-refreshing)
harness dashboard                 # http://127.0.0.1:8765

# Memory layer utilities
harness memory ingest                                   # ingest all logs' decisions
harness memory query-decisions "test suite"            # search decision memory
harness memory query-structure --repo . "callers run_task"   # code graph query
harness memory record "always pin litellm < 1.100 on py3.10"

# MCP server (stdio) — connect Claude Code / Cursor / any MCP client
python -m mcp_server
```

MCP tools exposed: `query_structure`, `query_decisions`,
`record_decision`, `task_status`, `list_repos`.

Useful flags on `fix`/`run-benchmark`: `--target-test` (pytest node id),
`--test-command`, `--max-retries`, `--budget` (USD cap), `--protected`
(repeatable globs), `--adaptive-routing` (difficulty-based tier selection),
`--api-base` (BYO router/gateway), `--log-root`. `--issue @file.txt` reads
the bug report from a file. Exit codes: 0 success / 1 task failure /
2 usage error.

### Benchmark subset JSON format

```json
[
  {
    "repo": "path/to/repo",
    "issue": "description of the bug (what's wrong, which test should pass)",
    "task_id": "optional-id",
    "target_test": "tests/test_x.py::test_y",
    "config": {"max_retries": 2, "budget_cap_usd": 0.50}
  }
]
```

## Repo layout

| Path | Owner | What it is |
|---|---|---|
| [`harness/`](harness/) | Terminal 1 | The agent loop: retrieval, planning, bash-only editing, verifier-gated completion, structured state + traces |
| [`execution/`](execution/) | Terminal 2 | Docker sandbox (fresh container per command, resource limits, no network by default), stateless `verify()` with flake detection, git-native output + rationale logs |
| [`runtime/`](runtime/) | Terminal 3 | Concurrent scheduler (worker subprocesses, checkpoint/resume, hang/crash supervision, approval gate), adaptive model router + difficulty predictor, ablation runner |
| [`memory/`](memory/) | Terminal 4 | Tree-sitter code graph + SQLite decision store, recursive state-file ingestion |
| [`mcp_server/`](mcp_server/) | Terminal 4 | MCP server exposing memory + task status over stdio |
| [`cli/`](cli/) | Terminal 4 | The `harness` CLI (fix / run-benchmark / status / dashboard / memory) |
| [`dashboard/`](dashboard/) | Terminal 4 | Read-only web dashboard over existing logs (stretch item 40) |
| [`shared/`](shared/) | all | Cross-module dataclasses (`Task`, `TaskResult`, ...) |
| [`tests/`](tests/) | all | 226 tests incl. 5 end-to-end bug-fix runs against the real Docker sandbox |

Per-module documentation (what's built, decisions, known issues):

- [`harness/AGENTS.md`](harness/AGENTS.md) — harness core & context management
- [`execution/AGENTS.md`](execution/AGENTS.md) — sandbox, verification, git output
- [`runtime/AGENTS.md`](runtime/AGENTS.md) — scheduler, routing, ablation results
- [`memory/AGENTS.md`](memory/AGENTS.md) — code graph + decision store
- [`mcp_server/AGENTS.md`](mcp_server/AGENTS.md) — MCP surface
- [`cli/AGENTS.md`](cli/AGENTS.md) — CLI usage & gotchas
- [`dashboard/AGENTS.md`](dashboard/AGENTS.md) — read-only web dashboard

## How a task flows through the system

```
issue + repo
  └─ harness.core.run_task (Boundary 3)         [Terminal 1]
       ├─ retrieval: grep + code-graph structural layer (memory/)
       ├─ planner → 2-4 sub-steps, each independently verified
       ├─ bash-only edit sessions against logs/{task_id}/work/ (never the original repo)
       ├─ execution.verify: target test reruns (flake check) + full suite (regression)
       │    └─ each command runs in a FRESH Docker container (Boundary 1)  [Terminal 2]
       ├─ failure feedback → next attempt (rollback + structured feedback)
       ├─ state.json (Boundary 4) + trace.jsonl + plan.json written under logs/{task_id}/
       └─ model access via runtime.model_router.call_model (Boundary 2)   [Terminal 3]
            └─ adaptive routing: predicted difficulty → cheap/expensive tier
runtime.scheduler.run  spawns one `python -m runtime.worker` per task,      [Terminal 3]
  supervises them (heartbeat/hang detection, crash budget, resume, approvals)
cli (Boundary 6)        harness fix | run-benchmark | status               [Terminal 4]
memory (Boundary 4/5)  ingests decisions from every state.json (recursive),
  serves code graph + decisions over MCP to any client
```

All structured state lives under `logs/` (gitignored): per-task
`state.json` (progress source of truth), `trace.jsonl` (every prompt,
response, tool call, result), `plan.json`, and the runtime's sibling
`{task_id}.runtime/` bookkeeping (checkpoints, model ledger).

## Conventions

- Type hints + docstrings on all public functions; docstrings state what a
  function does and what it assumes about its inputs.
- Config values (retries, budgets, model choices) ride on `Task.config`,
  never hardcoded — every run records its settings alongside its logs.
- Nothing under `logs/` is silently swallowed: every failure is logged.
- Cross-module contracts live in [`INTERFACES.md`](INTERFACES.md) (with a
  Change Log) — build against those, not other modules' internals.
- Commit messages: `[module] short description`.
