# mcp_server/ — Terminal 4: MCP Exposure (Boundary 5)

## What's built
- `mcp_server/server.py` — standalone MCP server (official Python SDK,
  **2.x** installed: `MCPServer`; 1.x `FastMCP` import kept as fallback so
  the file works on both SDK generations). Run: `python -m mcp_server`
  (stdio transport — connect Claude Code / Cursor / any MCP client).
- Tools (Boundary 5 contract + compatible extensions, logged in
  INTERFACES.md Change Log):
  - `query_structure(query, repo="")` — code-graph query. First call for a
    repo builds its index under `HARNESS_HOME/code-graph/`; omitted repo =
    last-indexed repo (pointer file). Verbs: see `memory/code_graph.py`
    QUERY_HELP ("callers X", "callees X", "importers M", "symbol X", ...).
  - `query_decisions(query="")` — ranked search of decision memory; every
    call first lazily ingests new `logs/*/state.json` decisions.
  - `record_decision(text, category="general") -> str` — append a fact.
  - `task_status(task_id)` — Boundary 4 state.json summary (spec item 31).
  - `list_repos()` — indexed repos + file counts.
- Decision DB + graph indexes resolve through `memory/paths.py` (env
  overridable, isolated in tests via `HARNESS_HOME`).
- Graph objects are cached per repo in-process (read-only after build);
  server-side state is thread-safe.

## Verified by
`tests/test_mcp_server.py` (8): tool registration, in-process calls, lazy
ingestion, graph indexing via tool, and a REAL stdio client round-trip —
server subprocess launched exactly as an external MCP client would, all
five tools exercised (initialize → list_tools → call_tool × 4).

## Round 2 — real-client verification (production data)
Beyond the test suite, exercised the server as an external host would
(stdio subprocess against the PRODUCTION `HARNESS_HOME` — real logs, real
repo index, no env isolation):
- `query_decisions "test suite"` → returns the real ingested decisions from
  T1's actual harness runs (incl. the Round-2 real fix/benchmark tasks).
- `query_structure "callers run_task"` → 20 real callers incl.
  `cli.main.cmd_fix` and all `tests/test_e2e_run_task.py` functions.
- `record_decision` → landed as row #22 in the production DB.
- `task_status "real-fix-bug02-cloud"` → real task state (1/1 steps,
  files touched, both decisions) rendered correctly.
- `list_repos` → this repo (87 files) marked last-used.
All five tools answered correctly from an external caller's perspective.

## Notes / decisions
- No background watcher thread: lazy poll-on-query is self-healing and
  simpler; `DecisionStore.watch()` exists for callers that want one.
- MCP SDK 2.x renamed `FastMCP`→`MCPServer` and `isError`→`is_error`;
  tests pin the 2.x behavior.
- `record_decision` returns a string instead of None (documented in the
  Change Log) — MCP tools always return content; silent success is
  client-hostile.

## Deferred
- No SSE/HTTP transport wired (stdio only — standard for local servers;
  `mcp.run("sse")` exists if needed later).
- No auth (local tool; anything that can spawn it can read the files anyway).

## Round 3 — unchanged, verified still green
No server changes this round. All 8 tests re-run green after T1's
context.py landed its resume work and after my Round-2 repair of the
same file (the production DB ingested the new stress/ablation state
files via the lazy poll — 27/27 decisions present, cross-checked).

## Round 4 — feature-inventory self-audit (items 23, 31)
- **Item 23 (memory exposed as MCP, cross-session AND cross-agent,
  queryable by external clients) — FULLY BUILT.** Verified three ways:
  the 8-test suite (incl. the REAL stdio-client round trip that launches
  this server exactly as Claude Code/Cursor would: initialize →
  list_tools → all five tools), the Round-2 production-data session
  (all five tools answered from a real external caller), and re-run
  green this round. Cross-session: DB + graph indexes live on disk and
  every query lazily ingests new state files; cross-agent: any process
  (harness worker, CLI, external client) shares the same store.
- **Item 31 (expose harness's own memory/status as MCP tools) — FULLY
  BUILT**: `query_structure`/`query_decisions` (memory) +
  `task_status`/`list_repos` (status), exactly the spec's wording.
- 8/8 tests re-run green post Round-3/4 changes (part of the 254-pass
  repo-wide re-verification).
- Related audit note: item 30 (CONSUME external MCP) — landed by the
  parallel T4 session the same evening (see memory/AGENTS.md); this
  server doubles as its test target.

## Round 5 (2026-09-09) — CLOSEOUT: served the final system test's queries

No server changes (none needed). The Round-5 full-stack e2e
(`logs/final-e2e/`: CLI → scheduler → harness → Docker → cloud model →
memory ingestion) verified this server's role live: after
`DecisionStore.poll` ingested the fresh run's 2 decisions,
`harness mcp call "python -m mcp_server" query_decisions` answered
them back over a real stdio round-trip (the CLI's mcp client —
memory/mcp_client.py — consuming this server exactly as an external
MCP tool would). 8/8 tests green in the module sweep.
