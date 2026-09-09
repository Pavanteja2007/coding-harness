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

### mcp_client.py — MCP CLIENT for external servers (Round 4, item 30)
- `list_mcp_tools(server, cwd?, env?)` / `call_mcp_tool(server, tool,
  args?, cwd?, env?)` — spawn an EXTERNAL MCP server over stdio
  (official SDK client half; one connection per call, no pooling to
  get wrong), list its tools / call one. Results are plain dicts
  (`{"ok", "text"/"tools", "error"}`) — never raises: spawn failures,
  unknown tools, tool errors come back as ok=False data.
- `parse_server_command` — shlex (non-posix, so Windows paths survive)
  with one matching quote-pair stripped per token (the literal quotes
  would break subprocess spawn — found live on Windows).
- Env: the SDK's `StdioServerParameters(env=None)` does NOT inherit the
  parent env — we pass `dict(os.environ)` by default (found live:
  isolated HARNESS_HOME tests failed without it).
- CLI surface: `harness mcp list-tools/call` (cli/main.py); our own
  mcp_server doubles as the test target — 12 tests
  (tests/test_mcp_client.py: real subprocess round-trips, bad
  server/tool → ok=False, quoted-path argv, CLI wrappers).

### mcp_client.py — MCP CLIENT half (spec item 30, Round 4)

(Superseded summary — the authoritative, fuller description is the FIRST
mcp_client section above; the Round-4 edit accidentally appended this
duplicate instead of replacing it. Kept only this pointer.)

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

## Round 4 — feature-inventory self-audit (items 21-23, 30)

Audited against `project-spec.md`'s inventory with fresh test runs:

- **Item 21 (structural code graph) — FULLY BUILT.** All node kinds
  (func/class/method/module/file with file:line + docstring) and edge
  kinds (calls/imports/defines) the spec names; all 9 query verbs;
  persisted index with mtime-based reuse; 13/13 tests green this round.
  One honest caveat for write-ups: the "verified on this repo" claim
  above is a manual run, not an encoded test (tests use synthetic
  repos); name-based call resolution is the documented over-approx.
- **Item 22 (decision/pattern memory) — FULLY BUILT.** record/search
  (ranked)/get/count; recursive Boundary-4 ingestion (unique-index
  dedupe → idempotent re-polls); watch(); thread-safe SQLite WAL;
  12/12 tests green this round.
- **Item 23 (exposed as MCP server) — FULLY BUILT** (see
  mcp_server/AGENTS.md): all five tools answer from a real external
  stdio client; persists across sessions AND agents by construction
  (DB + index on disk; every query lazily ingests new state files).
- **Item 30 (consume EXTERNAL MCP tools/connectors) — NOW BUILT, landed
  by the parallel Terminal-4 session DURING this audit (verified, not
  assumed): `memory/mcp_client.py` (stdio-only, one-call-per-connection,
  never raises — returns {"ok", ...} dicts) + `harness mcp call|list-tools`
  CLI subcommands; 12/12 `tests/test_mcp_client.py` green, live-checked
  here: `harness mcp list-tools "python -m mcp_server"` shows the five
  tools and `harness mcp call ... query_decisions` answers from the
  production DB. Our own server doubles as the test target (the spec's
  "where useful" — no external server was needed to prove the surface).
  My earlier same-day audit verdict said NOT BUILT; the module landed
  minutes later — recorded here so the audit trail stays honest.**
- Full-suite re-verified post Round-3/4 changes: 254 passed / 3 skipped
  repo-wide; production DB re-poll idempotent (189 rows, 0 new).

## Round 5 (2026-09-09) — CLOSEOUT: final full-stack e2e PASS through this module

The Round-5 system test (`logs/final-e2e/`, driver + report JSON) exercised
this module's two surfaces against a REAL fresh run with every other
module's closeout fix live:

- **Ingestion**: after the CLI→scheduler→harness→Docker→cloud-model run
  completed (success, 1 attempt), `DecisionStore.poll` on the run's log
  root ingested its 2 decisions with ZERO changes — the recursive poll
  + unique-index dedupe handled the custom `--log-root` layout (a
  Round-4 runtime fix made the fake path agree; the real path always
  did).
- **Serving**: `harness mcp call "python -m mcp_server" query_decisions
  {"query": "mean verified"}` answered from the DB over a real
  CLI→mcp_client→server stdio round-trip (the spec item-30 consume path
  doubling as the verification), and the search surface ranked the new
  run's decisions alongside the historical ones.
- Also verified: the duplicate mcp_client section in this file (a
  Round-4 edit artifact) collapsed to a pointer to the authoritative
  section; `mcp_client.py`'s docstring CLI signature fixed to match the
  real `--args` flag.
- Module tests re-run green post-changes (code_graph 13 + decision_store
  12 + mcp_client 12 + mcp_server 8 + cli 16 + dashboard — 45/45 in the
  module sweep; part of the repo-wide 300-pass state).

## Round 6 (2026-09-09) — adversarial hardening round: shared task-id containment guard + probes of every memory surface

**Context**: Terminal 4's Round-6 security pass adversarially tested the
MCP server and CLI (full probe matrices + outcome tables in
mcp_server/AGENTS.md and cli/AGENTS.md). The memory module's role in
that round: one REAL vulnerability lived in shared path-handling
semantics, and the fix belongs here so CLI + MCP server can't drift.

### New shared surface: `memory/paths.py::is_safe_task_id` / `safe_task_dir`

- **Why**: task ids are joined onto a logs root to find state.json.
  Empirically verified escape forms on Windows that naive joining
  permits: pathlib `/` DISCARDS the base for drive-lettered operands
  (`Path('logs')/'C:/evil'` → `C:\evil`); `C:evil` is drive-RELATIVE
  (same discard); `..`-chains resolve through; and Win32 path
  normalization makes `' ..'` resolve AS `..` and `'sub.'` alias
  `sub` (trailing spaces/dots stripped). Null bytes raise ValueError
  in `resolve()`.
- **The guard** (semantic, not a charset allowlist): rejects
  separators `/` `\`, null bytes, `:` (drive/UNC forms), edge
  whitespace (the `' ..'` trick), all-dot segments after
  Win32-equivalent normalization (trailing dots/spaces stripped
  before the `.`/`..` check); then belt-and-suspenders
  resolve-containment inside the logs root. Interior spaces/unicode/
  dots remain ALLOWED (legit user ids) — tested both directions.
- **Consumers**: `mcp_server.task_status` + `cli status --task-id`
  (both previously exploitable for arbitrary state.json reads —
  live-confirmed, details in their AGENTS.md files). If any future
  surface joins a task id onto a path, route through
  `safe_task_dir` — do not re-derive.

### Adversarial probes against THIS module's own surfaces (all contained)

- **DecisionStore.search SQL injection**: payloads (`DROP TABLE`,
  `OR '1'='1`, `UNION SELECT`, `%`-wildcards) → parameterized storage
  + Python-side substring ranking render them literal keywords; store
  provably intact after each payload (canary insert + count).
- **Secret harvesting**: production DB (200 decisions) swept with
  secret-pattern regex (api keys, Bearer, ghp_, AKIA, tokenrouter
  keys) → 0 hits. The store ingests `decisions` arrays from
  state.json — harness decisions, never credentials — and the
  harness's own trace redacts api_key from logged configs (verified
  in its task_start events).
- **DecisionStore ingestion scope**: canary state.json OUTSIDE the
  configured logs root is never ingested/served (poll is bounded by
  the root passed to it).
- **CodeGraph.query hostile args**: `file ../../Windows/win.ini` /
  `C:/Windows/win.ini` / `/etc/shadow` → matched only against
  INDEXED repo-relative names (clean miss); shell metacharacters
  inert; file CONTENT never served (structure + docstring FIRST
  LINES only — a module constant's VALUE is not in the graph).
  Null-byte paths raise inside `resolve()` → caught as clean errors
  by both server and CLI wrappers (no traceback).
- **CodeGraph arbitrary-dir indexing** (scope, accepted): the graph
  can index any directory the OPERATOR names — by design (same trust
  boundary as the operator's shell; the server docstring says local
  use). Documented in mcp_server/AGENTS.md Round 6.
- **mcp_client**: spawn-string handling (`parse_server_command`)
  rechecked under Round 6 — non-posix shlex + single quote-pair
  strip; no shell execution (subprocess argv list); a hostile server
  command fails to spawn → `{"ok": false}`, never a raise.

### Test status

- NEW: `tests/test_cli_adversarial.py` (62) +
  `tests/test_mcp_adversarial.py` (39) — the two boundary suites fed
  by this module's guard; both green.
- `tests/test_decision_store.py` (12), `tests/test_code_graph.py`
  (13), `tests/test_mcp_server.py` (8), `tests/test_mcp_client.py`
  (12), `tests/test_cli.py` (16), `tests/test_dashboard.py` (7)
  re-run green post-changes. Production DB re-polled idempotent
  post-adversarial-session (probe rows cleaned from the production
  DB after the audit run; 200 stable decisions).
- Production audit artifacts (real external-client + real-subprocess
  sessions, reports kept):
  `logs/mcp-adversarial/run_mcp_adversarial.py` (19/19 contained) and
  `logs/cli-adversarial/run_cli_adversarial.py` (38/38 contained).

## What's stubbed / deferred
- Python-only (project tech lock — Phase 1). Other grammars later.
- No incremental re-indexing: any .py mtime change = full rebuild (fine at
  medium scale; the mtime snapshot makes it self-healing).
- No embeddings/semantic search — keyword ranking only (ChromaDB-era
  retrieval is Terminal 1's harness context problem, not memory's).
- (Round 7) mcp_client passes an explicit errlog to the SDK's stdio
  transport — see the Round 7 section; do NOT "simplify" it away: the
  SDK default is poisonable under pytest capture on Windows.

## For the other terminals
- Terminal 1: `record_decision` after each task can be a direct
  `DecisionStore.record(...)` call or an MCP tool call — but your
  state.json `decisions` field is ALREADY auto-ingested by every
  `query_decisions` call, so direct calls are only needed for facts not in
  state files (e.g. cross-task conventions a human wants remembered).
- Everyone: `CodeGraph(repo).query(...)` is cheap and dependency-free
  beyond tree-sitter — the harness's retrieval step (Phase 2) may want to
  consume structural context from here instead of re-implementing greps.

## Round 7 (2026-09-09) — production readiness: CI + a real cross-platform flake fixed

### Task A — CI for memory/MCP/CLI (`.github/workflows/memory-cli-ci.yml`)

Separate workflow file BY DESIGN (Terminal 2 landed `.github/workflows/
ci.yml` for harness+runtime in parallel — separate files avoid collisions
on the shared CI surface). Two jobs:
- `memory-mcp`: code graph + decision store + MCP server/client
  (incl. the REAL stdio round-trip) + dashboard — Docker-light, so it
  runs the full ubuntu/windows/macos × 3.10/3.12 matrix (the Windows
  leg pins this module's Win32 path-guard semantics where they were
  empirically derived).
- `cli`: test_cli.py incl. the offline fix e2e (real run_task →
  scripted model → REAL Docker sandbox/verify) — ubuntu only (Docker
  preinstalled; the e2e needs the daemon), smoke_repo image warmed first.
Install is DIRECT (`pip install pytest anyio tree-sitter tree-sitter-python
"mcp>=1.2"`) not `pip install -e .` — the editable install needs the
explicit `[tool.setuptools] packages` list that is still uncommitted
pyproject work; tests import from the checkout root regardless. Validated
against the committed tree (b9ecd9c): 70/70 module tests green in a
stash-verified run.

### The flake CI would have shipped with: SDK `errlog` import-time binding

**Found by literally running the workflow's commands on loop before
committing them** (~1-in-4 randomized runs, 5-6 stdio tests failing
all-at-once per session with `UnsupportedOperation: fileno`):

- Root cause (verified by a standalone repro, not guessed): the MCP
  SDK binds `errlog: TextIO = sys.stderr` as a DEFAULT PARAMETER at
  import time (`mcp.os.win32.utilities.create_windows_process`). This
  module imports the SDK lazily inside the first spawning call, and
  pytest-randomly shuffles order — so when the first import happened
  inside a `capsys` test, `sys.stderr` was a fileno-less `CaptureIO`,
  and the poisoned default broke EVERY stdio spawn in the session.
- Fix (this module's, not the SDK's): `memory/mcp_client.py` now
  passes an explicit `errlog` (`_server_errlog()`: the interpreter's
  true `sys.__stderr__`, DEVNULL under pythonw) at its one
  `stdio_client` call site — spawns are independent of import order
  and capture state. The two test files that call `stdio_client`
  directly use the same helper.
- Pinned by `tests/test_mcp_stdio_fileno.py` (2): the sink-has-fileno
  invariant + the live bug condition (spawn under ACTIVE capsys).
- Verified: 10/10 + 6/6 previously-flaky batch combos green
  post-fix (0 failures where it was ~25% pre-fix).
- **For every terminal that spawns MCP stdio servers in tests**: pass
  an explicit `errlog`, never trust the SDK default — your suite is
  one random-order shuffle away from this if you lazy-import the SDK
  under capsys on Windows.

### Task B — CHANGELOG.md + v0.1.0 + CI badge

`CHANGELOG.md` (repo root): high-level milestones only (four layers,
adaptive-routing results with honesty notes, security hardening,
known limitations) — module detail stays in AGENTS.md files per
convention. Tag `v0.1.0` on the Round-7 commit.

## Tests (Round 7 state)
`tests/test_code_graph.py` (13), `tests/test_decision_store.py` (12),
`tests/test_mcp_server.py` (8), `tests/test_mcp_client.py` (12),
`tests/test_mcp_stdio_fileno.py` (2 NEW), `tests/test_dashboard.py` (7),
`tests/test_cli.py` (16) — all green; 111-test randomized batch combos
× 6 green (the flake-fix verification).
