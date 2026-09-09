# cli/ — Terminal 4: User-Facing CLI (Boundary 6)

## What's built
`cli/main.py` — the `harness` command (argparse; entry points: installed
console script, `python -m cli`). Subcommands:

- **`harness fix --repo <path> --issue <text|@file> [--task-id ...]`** →
  calls Terminal 1's REAL `harness.core.run_task` directly (single-task
  mode, no scheduler). Config flags (`--model --provider --api-key
  --target-test --test-command --max-retries --budget --protected
  --log-root`) map into `Task.config` per project convention. Prints a
  result summary (status, attempts, cost, verification flags, diff, trace
  path). Exit codes: 0 success, 1 task failure, 2 usage error.
- **`harness run-benchmark --subset <name|file.json> --concurrency <n>`**
  → calls Terminal 3's REAL `runtime.scheduler.run` (worker subprocesses,
  checkpoint/resume, supervision). `_call_scheduler` signature-probes:
  real scheduler `(tasks, concurrency, logs_root) -> dict` is normalized to
  task order; the local stub (below) stays importable for pre-T3 testing.
  Subsets: `smoke` (offline plumbing check via `use_fake_harness`, per
  runtime's documented config) or a JSON file
  `[{"repo", "issue", "task_id", "config", "target_test"}]` — SWE-bench
  loader is future work (spec defers it).
- **`harness status --task-id <id>`** → renders
  `logs/{task_id}/state.json` (plan checklist, files touched, decisions,
  remaining) + enriches status/cost/note from `trace.jsonl`'s last
  `result`/`task_end` event. Lists available task dirs when the id is
  unknown.
- **`harness memory record|query-deisions|query-structure|ingest`** —
  thin wrappers over the memory layer for demos/smoke (MCP remains the
  programmatic interface).
- **`harness dashboard [--logs-dir ...] [--host] [--port]
  [--refresh-s] [--no-browser]`** (Round 3, spec item 40) — serves the
  read-only web dashboard over existing logs; blocks until Ctrl+C.
  See `dashboard/AGENTS.md`.
- **`harness mcp list-tools "<server cmd>"` / `harness mcp call
  "<server cmd>" <tool> [--args '{...}'] [--cwd <dir>]`** (Round 4,
  spec item 30) — consume any EXTERNAL MCP server over stdio via
  `memory/mcp_client.py` (official SDK client). Results print as text;
  spawn/tool errors → clean stderr message + exit 1, never a traceback.
  Verified: our own `python -m mcp_server` doubles as the external
  target (tests/test_mcp_client.py, 12).

`cli/deps.py` — dependency resolution mirroring `harness/deps.py`:
override → real module → local stub. `run_task` has no stub (T1's real one
is on disk); `scheduler.run` falls back to `cli/_stubs/scheduler.py`
(threads + fan-out, same contract) if `runtime.scheduler` is ever absent.

`cli/fixtures/smoke_repo/` — tiny repo with a real bug (`mean()` returns
sum) + failing test; used by the `smoke` subset and CLI e2e tests.

## Verified by
`tests/test_cli.py` (16): parser, status rendering/enrichment/missing-id,
memory subcommands, full OFFLINE fix e2e (real run_task + scripted model
injected via `harness.deps.set_call_model` — fixes the bug, verifier
passes, diff printed), `@file` issue reading, model-failure → clean
structured error, run-benchmark through the REAL scheduler (subprocess
workers, offline fake harness), unknown-subset handling, stub fan-out +
crash isolation.

## Round 2 — REAL integration results (no stubs, no injected models)
- **`harness fix` e2e (real everything)**: fixed bug02_mean via the real
  `harness.core.run_task` + real Docker-sandboxed verify + REAL cloud
  model (z-ai/glm-5.3-free @ tokenrouter, BYO key) → `success`, 1 attempt,
  7 model calls, $0.0093, 116s, correct minimal diff (divisor fix), real
  state.json + trace.jsonl. A real Ollama qwen2.5:0.5b run was also
  attempted: plumbing perfect (verifier-gated failure, no false success) —
  the 0.5b model just replies SUBMIT without acting; capability limit,
  not a harness bug.
- **`harness run-benchmark` e2e (real everything)**: JSON subset → real
  `runtime.scheduler.run` → subprocess worker → real harness + real cloud
  model → `success 1/1`, $0.0031, 87s.
- **Gotchas found & fixed during real integration**:
  - Workers' default `hang_heartbeat_stale_s` (30s) kills the real harness
    during long model calls (T3's documented granularity limit). Fix on
    the caller side: set `hang_heartbeat_stale_s` in benchmark task
    configs for real-model runs — **Round-4 update (T3's measurement):
    300 is BORDERLINE when the cheap tier is loaded (a first planner
    call ran >300s and got hang-killed, requeued, finished on relaunch);
    use 600+ for real-model runs, 1200 for ablation-scale loads.**
  - `--api-base` flag added (maps to Task.config.api_base → router context
    set by `_set_router_context`, mirroring runtime.worker's exact pattern)
    — in-process fix runs against custom endpoints (BYO routers) work
    without a worker. Also `--adaptive-routing` flag added.
  - Subset JSON loader now reads `utf-8-sig` (PowerShell 5.1 writes BOMs).
  - Repaired `harness/context.py` mid-integration (T1's interrupted edit
    left a dangling `def _write` stub → IndentationError repo-wide);
    minimal deletion only, logged in INTERFACES.md Change Log.

## Notes / decisions
- Offline testing: `fix` is in-process (model injectable); benchmark workers
  are subprocesses (model NOT injectable in-process) — benchmarks go
  offline via `use_fake_harness` in Task.config. Documented in the
  Change Log.
- Exit code contract: 0 ok / 1 task-level failure / 2 usage error — keep
  when adding commands.
- `--issue @file` reads the bug report from a file.
- Router context for in-process fix runs is set/cleared around run_task
  (`_set_router_context` / `_clear_router_context`) — best-effort, no-op
  when runtime's router is absent (stub phase).

## Round 4 — README restored, demo packaged, audit clean

- **Root README.md was accidentally emptied** (commit 983f532 "Updated
  README.md" = 159 deletions, 0 additions — Round 3's content lost).
  Restored from the initial commit and updated for Round 3/4 reality:
  git-native output + rationale bullets, approval mode, DoD-on-real-OSS
  (python-semver), BOTH ablation scales with their honesty notes,
  corrected test count (257), demo section, and a "Status / what's
  honest" section (incl. the item-30 gap). Check `git diff README.md`
  before your next commit — if it still shows the 159-line deletion,
  restore from this version.
- **`demo/` packaged (Task D)**: `demo/run_demo.py` — one command, no
  API key/Docker, deterministic (scripted model + local sandbox stub;
  the harness loop, verify gating, git output, rationale, memory
  ingestion, and MCP-surface queries are all the REAL code paths).
  Isolated under `demo/demo-work/` (gitignored) via HARNESS_HOME +
  --log-root; production `.harness/` verified untouched. Reference a
  real-model variant + per-step talking points in `demo/README.md`.
  Verified: exit 0, artifacts present (git.json/rationale.md/state/
  trace), ran twice for idempotence.
- **Task A audit (this module's inventory lines)**: Boundary 6 CLI —
  fix / run-benchmark / status / memory / dashboard all live against
  real modules; 16/16 CLI tests green in the 254-pass repo-wide re-run.
- No CLI code changes this round (the demo drives it as-is).

## Round 5 (2026-09-09) — CLOSEOUT: final full-stack e2e PASS + doc-sync fixes

- **Task A (the final system test) — PASS.** Drove the COMPLETE real
  pipeline through this module's own entrypoint: `python -m cli
  run-benchmark` (driver `logs/final-e2e/run_final_e2e.py`, resolves
  TOKENROUTER_API_KEY into the subset config — the ablation's pattern)
  → real scheduler → worker subprocess → real harness (RECALL + the
  exhausted-turns state fix live) → real Docker sandbox/verify (T2's
  three-valued flake fix) → real cloud model → success, 1 attempt,
  $0.0131, 116s. Verified: state.json 1/1 steps complete (the T3-flagged
  flag class stayed fixed), git.json + two-commit work/ repo (fix diff
  IS the fix), rationale.md grounded, original fixture untouched,
  memory ingested (recursive poll, 2 decisions), MCP query answered via
  the real CLI→stdio round-trip, `harness status` renders true progress.
  Report: `logs/final-e2e/final_e2e_report.json`.
  Honest note: the FIRST run failed — but for the right reason: my
  driver's subset named a nonexistent target test (`test_mean` vs the
  fixture's real `test_mean_even_count`); the MODEL fixed the bug
  anyway and the verifier-gate correctly refused success on a bad
  target. Verifier gating worked as designed; driver fixed + re-run.
- **Task B (doc-sync)**: `hang_heartbeat_stale_s` guidance updated to
  runtime's Round-5 measurement (300 borderline under load; 600+ for
  real-model runs, 1200 ablation-scale); stale "stretch item 40"
  dashboard labels dropped (the stretch framing was retired in R4).
- `tests/test_cli.py` + mcp/dashboard suites re-run green this round
  (45/45 module tests); demo re-run green post-fixes.

## Deferred
- SWE-bench Lite subset loader (spec: deferred, not a blocker).
- Global `--json` output mode (human-readable only for now).
- Installing the `harness` console script needs `pip install -e .` — tests
  call `cli.main.main(argv)` directly instead.
