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

## Round 6 (2026-09-09) — adversarial security testing: ONE REAL LEAK found, fixed, and pinned by tests

**Verdict up front: after hardening, every adversarial probe is
contained — 39/39 adversarial tests green (tests/test_mcp_adversarial.py)
plus a 19/19 contained production-data session over the REAL stdio
transport (logs/mcp-adversarial/, report JSON kept). But the round
STARTED with a live-confirmed data-leak vulnerability: `task_status`
was exploitable for arbitrary state.json reads.**

### The real finding: path traversal in `task_status` (FOUND LIVE, FIXED)

- **The bug**: `task_status(task_id)` built its path as
  `default_logs_dir() / task_id / "state.json"` with NO validation.
  Two Windows-verified escape forms: (a) pathlib's `/` operator
  DISCARDS the base for a second operand with a drive letter —
  `Path('logs') / 'C:/Users/.../final-e2e-bug02'` → `C:\Users\...\final-e2e-bug02`;
  (b) plain `..`-chains resolve through. **Reproduced live before
  fixing**: with HARNESS_LOGS_DIR pointed at an empty temp dir, both
  forms returned a REAL production task's full state (plan, files,
  decisions) — a genuine out-of-scope data leak, not a theoretical one.
- **The fix**: `task_status` now resolves task ids only through
  `memory.paths.safe_task_dir()` (new shared guard, see
  memory/AGENTS.md): rejects separators (`/` `\`), null bytes,
  drive forms via `:` (`C:` is drive-RELATIVE on Win32 — discards the
  base), edge-whitespace dot tricks (`' ..'` resolves AS `..` under
  Win32 normalization — verified empirically), pure-dot segments —
  then belt-and-suspenders: the resolved path must still be inside
  the logs root. Odd-but-benign ids (interior spaces/unicode/dots)
  still work — the guard is semantic, not a charset allowlist.
- **Regression pinned**: `test_stdio_traversal_rejected_over_real_transport`
  replays the exact exploit (absolute + ..-chain forms) through a REAL
  external stdio client session and asserts the outside-logs canary
  never appears. `test_task_status_traversal_absolute_and_relative_forms`
  covers all escape variants in-process.

### Every adversarial probe tried, and its outcome (all CONTAINED)

1. **task_status path traversal** — the one real leak (above). Post-fix:
   absolute paths, `..`-chains, drive forms (`C:/`, `C:x`, `C:`),
   separators, null bytes, whitespace-dot tricks → all rejected with
   "invalid task id", outside-logs canary never rendered; legit ids
   still served.
2. **query_decisions SQL injection** — `'; DROP TABLE decisions; --`,
   `x' OR '1'='1`, `UNION SELECT`, `%`-wildcard payloads → all treated
   as literal keywords (storage is parameterized; ranking is Python
   substring). Store provably intact after every payload (canary
   insert + count checks). No data bypass possible: ranking only
   re-orders rows the query legitimately matched.
3. **query_decisions secret harvest** — queries for "api key",
   "secret", "token", "password", "credential", "sk-", "Bearer",
   "TOKENROUTER", "litellm key" against the PRODUCTION DB (200 rows):
   zero secret-pattern hits (regex sweep over all 200 texts).
   Decision memory stores harness decisions, never credentials.
4. **query_decisions scope** — a canary state.json planted OUTSIDE the
   logs root is never ingested or served (poll scans the configured
   logs root only).
5. **query_structure hostile queries** — `file ../../Windows/win.ini`,
   `file C:/Windows/win.ini`, `file /etc/shadow`, `symbols $(reboot)`:
   traversal args only ever match INDEXED repo-relative paths (miss);
   shell metacharacters are inert parse text; no host file content
   ever rendered (`[fonts]`/`[boot]`/`root:` markers absent).
6. **query_structure hostile repo args** — null-byte path → SDK
   generic `Error executing tool` (no traceback leak); nonexistent
   path → clean "no code-graph index" miss.
7. **query_structure arbitrary-directory indexing (SCOPE, accepted &
   documented)**: a client CAN point `repo=` at any local directory
   and index it. Deliberate scope decision, not a vuln: this is a
   LOCAL server — anyone who can spawn it can read those files
   directly (same trust boundary as the operator's own shell). The
   verified bound: only STRUCTURE is served (symbol names, file
   paths, docstring first lines) — never file CONTENT (tested: a
   module-level constant value is not served; only its name is).
8. **record_decision hostile payloads** — `DROP TABLE`/`$(calc)`/
   `` `reboot` `` text stored verbatim, returned verbatim on search,
   never executed; DB intact.
9. **SDK type confusion** — repo as int, query as list → SDK schema
   validation rejects before the tool body runs (ToolError, no leak).
10. **Traceback exposure** — the MCP SDK 2.x wraps every tool exception
    as `Error executing tool <name>` (verified in SDK source:
    `Tool.run` re-raises with cause stripped for unexpected
    exceptions) — clients never see file paths from tracebacks.

**Where the guarantees live**: the task-id guard is SHARED code at
`memory/paths.py::safe_task_dir/is_safe_task_id` (one implementation,
used by both this server and the CLI — see cli/AGENTS.md). Server-side
containment tests: tests/test_mcp_adversarial.py (39). Production-data
audit artifact: `logs/mcp-adversarial/run_mcp_adversarial.py` +
`mcp_adversarial_report.json` (19/19 contained, incl. the exploit
replay against real logs).

**No contract changes**: tool signatures unchanged; the only behavior
change is that traversal-shaped task_ids now get a rejection string
instead of out-of-scope data.

## Round 7 (2026-09-09) — production readiness

- **CI**: this server's suite (incl. the REAL stdio round-trip) runs on
  every push in `.github/workflows/memory-cli-ci.yml` — separate file
  from Terminal 2's ci.yml by design; see memory/AGENTS.md Round 7 for
  the full job layout and the errlog-flake fix that made the round-trip
  deterministic under randomized test order.
- **CHANGELOG.md + v0.1.0**: this module's Round-6 security verdict is
  summarized there; probe detail stays here (as before).
- No server-code changes this round (39/39 adversarial + 8/8 server
  tests re-run green post the shared mcp_client errlog fix — the server
  itself was never affected; only the client-side spawn path was).
