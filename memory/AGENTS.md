# memory/ — Terminal 4: Persistent Memory Layer

Structural code memory + decision/pattern memory, the "cross-session brain"
of the project (spec items 21-23). Exposed externally via `mcp_server/`
(Boundary 5); consumed directly by `cli/`.

## What's built

### code_graph.py — structural code memory (tree-sitter)
- `CodeGraphBuilder(repo_path).build()` walks a repo (`.py` files; skips
  `__pycache__`, `.git`, `venv`, `logs`, etc.), parses with tree-sitter,
  and produces a `Graph`: nodes (funcs / classes / methods / modules /
  files, all with file:line + first docstring line) + `calls` / `imports` /
  `defines` edges.
- `CodeGraph(repo_path)` — persistent, queryable wrapper. Default index
  root `HARNESS_HOME/code-graph/<sanitized-abs-path>/` (one dir per repo,
  so multiple repos coexist); `build()` saves graph.json + meta.json,
  `load_or_build()` reuses a fresh index (mtime snapshot match) or rebuilds.
- Query surface via `query(str)` / MCP `query_structure`: `symbol <name>`,
  `callers <name>`, `callees <name>`, `importers <module>`, `imports
  <module>`, `file <path>`, `files [pat]`, `symbols [pat]`, `help`.
  Unknown input degrades to the help text, never a traceback.
- Verified on this repo: **87 files, 800+ nodes, 1100+ call edges indexed
  in ~1s**; `callers run_task` correctly resolves test functions + `cli.main.
  cmd_fix` etc.

Known over-approximation (deliberate, documented in the module docstring):
Python call resolution is name-based — `obj.foo()` edges to ANY method named
`foo` regardless of receiver type. Right trade for a structural read model;
do not present it as type-accurate call-graph semantics in write-ups.

### decision_store.py — decision/pattern memory (SQLite)
- `DecisionStore(db_path)` — thread-safe store. `record(text, category,
  source, task_id, repo_path, dedupe)`, ranked keyword `search(query,
  limit)` (rank = matched-word count, then recency; empty query = recent),
  `get(id)`, `count(source)`.
- Boundary 4 ingestion: `ingest_state_file(path)` (reads `decisions` from a
  `state.json`; unique index on (task_id, text) makes re-ingesting a
  rewritten/grown state file a no-op), `poll(logs_dir)` (all task dirs),
  `watch(logs_dir, interval, stop_event)` (background thread for
  long-running processes).
- **`poll` is RECURSIVE (rglob)** — real runs nest state files deeper than
  `logs/{task_id}/` (T3's ablation driver stages them at
  `logs/ablations/<run>/tasklogs/<task_id}/state.json`); the old 1-level
  glob missed those (Round 2 fix).
- Default DB: `HARNESS_HOME/memory/decisions.db` (WAL mode; gitignored).

### paths.py — location conventions
`harness_home()` / `decisions_db_path()` / `default_logs_dir()` — env
overridable (`HARNESS_HOME`, `HARNESS_DECISIONS_DB`, `HARNESS_LOGS_DIR`);
CLI and MCP server both resolve locations through here so they always agree.

## Round 2 integration results (real data, not synthetic)
- Ingested the REAL `logs/` tree Terminal 1's harness actually produced:
  20 state.json files (incl. nested ablation layouts + this repo's own
  real fix/benchmark runs), 10 real decisions, all landed in the DB;
  cross-check script confirmed every on-disk decision is in the store.
- Runs with 0 decisions (abl-bug03/bug04 offline) are correct behavior:
  T1 records decisions only on verified fixes/early-exits — incomplete
  runs legitimately have none.
- Re-ingestion after `harness fix` / `run-benchmark` runs: no-ops for
  already-stored texts (unique index), new tasks' decisions land.
- `harness status`, MCP `task_status`, `query_decisions` all render the
  real runs' state/diffs/decisions correctly.

## Round 3 — Task B verification (expanded ablation scale)
Terminal 3's expanded (15-20 task) ablation run had NOT landed on disk
yet when this round started (newest state.json under logs/ablations/ was
still from the 5-task-per-arm runs — verified, timestamps checked), so
Task B was verified two honest ways:
- **Permanent regression test at expanded scale**:
  `test_poll_expanded_ablation_run` — a synthetic 20-task ON arm + 15-task
  OFF arm in T3's exact nested layout
  (`ablations/<run>/tasklogs/<task_id>/state.json`), with archived
  `.old-*` dirs, zero-decision tasks, and repeated task_ids across runs.
  poll() ingests every decision exactly once; second scan = 0 new.
- **Production tree at larger-than-target scale**: full-tree poll found
  **250 real state.json files** (incl. three 50-task stress runs — 2.5x
  the 15-20 target), **27/27** unique (task_id, decision) pairs now in
  the production DB (cross-checked against an independent on-disk
  ground-truth scan: 0 missing), re-poll idempotent (0 new).
When T3's expanded run lands, the recursive poll (rglob at any depth)
picks it up with no changes — that's the whole point of the Round-2 fix.

## What's stubbed / deferred
- Python-only (project tech lock — Phase 1). Other grammars later.
- No incremental re-indexing: any .py mtime change = full rebuild (fine at
  medium scale; the mtime snapshot makes it self-healing).
- No embeddings/semantic search — keyword ranking only (ChromaDB-era
  retrieval is Terminal 1's harness context problem, not memory's).

## For the other terminals
- Terminal 1: `record_decision` after each task can be a direct
  `DecisionStore.record(...)` call or an MCP tool call — but your
  state.json `decisions` field is ALREADY auto-ingested by every
  `query_decisions` call, so direct calls are only needed for facts not in
  state files (e.g. cross-task conventions a human wants remembered).
- Everyone: `CodeGraph(repo).query(...)` is cheap and dependency-free
  beyond tree-sitter — the harness's retrieval step (Phase 2) may want to
  consume structural context from here instead of re-implementing greps.

## Tests
`tests/test_code_graph.py` (13), `tests/test_decision_store.py` (12 —
incl. recursive-nested-tasklogs + expanded-ablation-scale regression
tests) — indexing, call/import edges, queries, persistence round-trip,
dedupe, watcher, malformed-state-file safety.
