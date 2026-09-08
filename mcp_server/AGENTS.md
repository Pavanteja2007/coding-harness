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
