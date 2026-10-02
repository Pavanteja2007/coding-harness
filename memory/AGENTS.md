# memory/ — Terminal 4: Persistent Memory Layer

## Multi-language round (2026-09-21) — JS/TS code-graph indexing (undocumented until 2026-09-22 audit)

*(No AGENTS.md entry was written when this landed; reconstructed from the
working-tree diff on 2026-09-22. No behavior changed by this entry.)*

- **`memory/code_graph.py` (+~420 lines)**: `_JSFileIndexer` walks
  tree-sitter JS/TS ASTs (`.js/.jsx/.mjs/.cjs/.ts/.tsx`, new
  `tree-sitter-javascript` + `tree-sitter-typescript` deps in
  pyproject.toml): classes/methods, arrow-function consts,
  `import`/`require` specifiers (`_js_normalize_specifier`, relative
  resolution via `_js_module_name`), re-exports, cross-file call edges
  (`_resolve_calls`). JSDoc leading comments kept as doc hints.
- Downstream (no new surfaces): retrieval anchoring, editor/lint syntax
  checks, agent-tests sanitize, and prompt flavoring all work on JS/TS
  repos through the existing Graph/NodeInfo shapes — no contract change
  for harness consumers.
- **Tests**: `tests/test_multilang_graph.py` (offline: indexing,
  anchoring, syntax checks, sanitize, flavoring on a synthetic JS repo).
- **Scope note**: same staleness as execution — `project-spec.md` still
  lists multi-language support as out of scope; flagged for amendment.

Structural code memory + decision/pattern memory, the "cross-session brain"
of the project (spec items 21-23). Exposed externally via `mcp_server/`
(Boundary 5); consumed directly by `cli/`.

## Improvement Round 2 (2026-09-10) — memory-informed planning (T1+T4 joint)

**The gap this round closed**: the store recorded decisions and was
queryable (MCP `query_decisions`), but nothing in the harness CONSUMED
them at planning time — Terminal 1's planner now queries this store
before every plan. See harness/AGENTS.md (same header) for the harness
half; this section covers the memory-module surface changes + the
ablation that measured the whole thing.

### Surface changes (additive; INTERFACES.md Change Log 2026-09-10)

- **`DecisionStore.search(query, limit, repo_path=None)`** — optional
  repo scoping. When set, only rows recorded with the SAME repo are
  returned (rows with no repo_path are excluded while the filter is
  on). Comparison is via `_repo_key()`: `os.path.normcase(str(Path(p)
  .resolve()))` — so a row recorded with an absolute path matches a
  query passing the same repo as a relative path (found live in the
  first test run: the pilot matched 0 rows because stored rows held
  absolute paths and the SQL branch compared exact strings; both the
  SQL empty-query branch and the ranking branch now normalize BOTH
  sides). Back-compat: no repo_path → previous behavior exactly.
- **`memory.decision_store.open_default_store()`** — the single opener
  every consumer should use (harness planner, MCP server, CLI):
  resolves `memory.paths.decisions_db_path()` (HARNESS_HOME /
  HARNESS_DECISIONS_DB env). The harness's `get_decision_store_
  factory()` resolves this real-first; the memplan ablation pins
  HARNESS_DECISIONS_DB per-run to a DEDICATED db so seeded rows never
  touch production memory.
- **Ingestion now has something to stamp**: T1's state.json writes the
  ADDITIVE `repo_path` key (Boundary 4 consumers must ignore unknown
  keys; `ingest_state_file` has read `data.get("repo_path")` since
  Round 1 — the reader predated the writer). State-file-sourced rows
  now carry the repo they belong to, so future planner queries scope
  correctly for REAL runs, not just seeded ones.

### The ablation (Task B, honest result)

Driver: `runtime/ablation_memplan.py` (T1 file, mirrors runtime/
ablation.py conventions). 5 fixture tasks, real full stack both arms,
same pinned model (z-ai/glm-5.3-free, adaptive routing OFF), arms
differ only in `plan_with_memory`. Prior decisions SEEDED via
`DecisionStore.record` (source "manual", repo_path stamped) from
genuinely mined prior-run traces (v4/v3): the bare-`pytest` ImportError
class that burned real turns in abl-off-bug01's attempt 1,
sed-with-quotes breakage from bug03 traces, per-repo suite-invocation
conventions, and what prior successful fixes actually did —
convention/gotcha facts, NOT fix cheat-sheets (the documented purpose
of manual records: "facts not in state files").

**Result (logs/memplan-ablations/run1/summary.json)**: 100% success /
1 attempt / 0 verify failures BOTH arms — no task-solving-power delta
on this set. Efficiency: harness-level model calls 35→27 (-23%),
tokens 147,916→112,390 (-24%), proxy cost $0.2148→$0.1782 (-17%).
Mistake recurrence: 3 OFF tasks re-tripped the seeded bare-pytest
ImportError class; 0 ON tasks did — the ON arm's first commands were
`python -m pytest` from the repo root, matching the seeded convention,
and its plans visibly mirror the seeded rows (causality evidence, not
just aggregate deltas). Honest caveats in the run's
post_run_analysis: n=5×1rep directional only; ledger "calls" inflated
by router-internal empty-response retries (recounted from trace
model_request events); one symmetric mid-run crash-retry per arm; a
parallel session's agent-tests feature ran in both arms.

### Tests

New: `tests/test_decision_memory_planning.py` (17, T1 file — covers
BOTH sides of the boundary: this module's repo filter/normalization/
opener location, the harness query/render/prompt-placement/predictor-
invisibility, and the ingest round-trip over real state.json +
repo_path). This module's own suites re-run green post-change:
decision_store 12 + code_graph 13 + mcp_server 8 + mcp_client 12 +
mcp_stdio_fileno 2 + dashboard 7 = 56/56; cli + adversarial 117/117.

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
- Env: the SDK's `StdioServerParameters(env=None)` does not inherit the
  parent environment. The default child environment is an explicit
  allowlist (process/runtime paths plus Neo/HARNESS state roots); provider
  credentials and other secret-shaped variables are not forwarded.
  Callers that intentionally need a server-specific variable must pass an
  explicit `env` mapping.
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
  `DecisionStore.record(...)` call or an MCP tool call - but your
  state.json `decisions` field is ALREADY auto-ingested by every
  `query_decisions` call, so direct calls are only needed for facts not in
  state files (e.g. cross-task conventions a human wants remembered).
  **(Improvement Round 2) Your PLANNER is now a live consumer**: it
  calls `open_default_store().search(query, limit, repo_path=<task
  repo>)` before every plan and injects the rows into its prompt. Two
  asks: (a) record decisions with `repo_path` set whenever the fact is
  repo-scoped (state.json's additive repo_path key now feeds ingestion
  automatically), (b) keep `search`'s signature/back-compat + the
  `open_default_store` path stable — the harness degrades gracefully
  but silently losing memory is worse than a Change Log flag.
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
"mcp>=2.0,<3"`) not `pip install -e .` — the editable install needs the
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

## Product round 2026-09-24 — durable context and scoped memory

- Added `memory/project_context.py`, a read-only project-instruction loader
  and token-budgeted context bundle. It reports loaded/omitted instruction
  files, precedence, source sizes, active/raw turn counts, and a status
  formatter. Root, nested, and `.neo` instruction files are deterministic;
  symlink/outside-repository files are refused. Instructions never mutate
  protected-path policy or decision-memory state.
- `cli/session.py` now owns strict atomic snapshots, raw-turn retention,
  repeated-compaction summaries, explicit corruption errors, repository
  identity checks, restart helpers, and metadata setters. The session API
  exposes `load_latest_session`, `retrieve_session_turns`,
  `build_session_context`, and `format_session_context_status` for the
  shell/agent owners.
- `DecisionStore` now rejects high-confidence secrets, scrubs legacy rows,
  scopes reads/writes by canonical repository, and uses `BEGIN IMMEDIATE`
  for cross-instance dedupe. `CodeGraph` writes graph/meta snapshots
  atomically and reports indexed-file counts separately from scanned files.
- Verification: exact required command
  `python -m pytest tests/test_cli_session.py tests/test_decision_store.py tests/test_code_graph.py tests/test_mcp_server.py tests/test_mcp_client.py tests/test_mcp_stdio_fileno.py tests/test_mcp_adversarial.py tests/test_skills.py -q`
  returned exit 0, 149 passed, 1 skipped; report:
  `C:\Users\pavan\AppData\Local\Temp\opencode\neo-t4-final-suite-534d20636e08471d82ab149ccfd346f4\required-suite.txt`.
  The skip is the Windows state-file symlink test (`WinError 1314`), not a
  Docker skip. Ruff check passed on every owned changed Python file.
- Blockers/requests: Prompt 1/2 must wire `load_latest_session` and
  `build_session_context` into the REPL/TUI and general agent prompt, and
  must render the context status receipt; those files were not edited here.
  The additive `repo_path` arguments on MCP decision tools need the
  contract owner's `INTERFACES.md` reconciliation; that file is explicitly
  out of this terminal's scope.

## Product round security follow-up 2026-09-24

- `project_context.py` now bounds instruction reads and truncation markers,
  reserves space for mandatory project instructions, propagates omitted and
  warning receipts, redacts rendered sources, and fails closed rather than
  retrying an unscoped legacy decision-store query.
- `DecisionStore` rejects quoted JSON credentials as well as common token
  formats; code-graph indexing refuses symlinked source files and records a
  graph digest in metadata so mismatched graph/meta generations rebuild.
- Required command after these changes: the plain Docker-backed lane is
  blocked by the unavailable daemon (154 passed, 3 baseline-verify failures,
  3 Windows symlink skips); the documented
  `HARNESS_EXEC_SKIP_DOCKER=1` lane is 157 passed, 3 Windows symlink skips,
  exit 0. Report/status: `logs/product-round/terminal-4.json`.
- Terminal 4 release reconciliation re-ran the expanded dashboard,
  decision-store, code-graph, MCP client/server, stdio-fileno,
  adversarial, and new release suites together: exit 0, 116 passed, 4
  skipped. All four skips are Windows symlink-privilege cases, not
  passes. Targeted Ruff is clean. The wheel/sdist installed-user flow
  is covered by `tests/test_installed_user_flow.py` and passed 2/2.

## VEX-ARCH-04 durable memory and checkpoint adapters (2026-09-25)

- `decision_store.py` now has explicit provenance-gated promotion, durable
  versus quarantine rows, bounded/redacted metadata, typed health, and
  non-destructive recovery reporting. Boundary-4 `state.json` decisions remain
  an explicit ingestion channel; session summaries are not auto-promoted.
- `checkpoints.py` and `checkpoint_store.py` provide a shadow-Git/worktree
  snapshot adapter, immutable file manifest, checkpoint review/diff, and
  preflighted restore that refuses later workspace or conversation edits.
- `mcp_client.py` exposes a reusable typed `McpClient` lifecycle with bounded
  operations, health metadata, and compatibility wrappers. The MCP server
  keeps exactly five tools and adds only a non-tool local health projection.
- Verification: decision-store tests 20 passed; session/checkpoint tests 37
  passed; the required six-file command returned 124 passed and one Windows
  symlink skip. Docker and live-provider lanes were not selected.
- Cross-owner handoff: harness/runtime mutation paths must call
  `memory.checkpoints.checkpoint_before_mutation` before dispatching writes;
  CLI connector health should consume the new client lifecycle rather than its
  daemon-thread probe. These files were not edited by this terminal.

### Final audit additions

- Checkpoint metadata now validates workspace identity and manifest shape,
  verifies snapshot hashes before restore, rejects symlinked storage/path
  components, excludes a workspace-contained checkpoint log root, and honors
  selected-file scope without blocking unrelated user edits.
- Session fresh recovery quarantines both corrupt snapshots and event journals;
  compatibility loads remain explicit, and imports reject malformed event
  journals while preserving compare-and-swap revisions on overwrite.
- The synchronous MCP facade now cancels timed-out worker tasks and closes its
  event loop after a bounded grace period. Final required verification is
  `132 passed, 1 skipped`; scoped Ruff and `compileall` are clean.

## VEX-ARCH-03 context-engine graph hardening (2026-09-25)

- `memory/code_graph.py` now stores per-file SHA-256 source digests alongside
  mtimes and a path-independent structural graph digest. A same-size edit with
  a preserved mtime rebuilds the index; corrupt graph metadata rebuilds rather
  than raising.
- Mixed Python/JavaScript/TypeScript files receive collision-safe symbol and
  module IDs, while Python retains the historical unsuffixed IDs. Exact source
  ranges, graph source digests, and sorted module-query results are public
  helpers for the harness context compiler. JSDoc extraction is now backed by
  source-line inspection rather than the previous empty placeholder.
- The graph remains a structural, name-based over-approximation. No embedding
  or semantic retrieval is claimed. `Graph.canonical_defines` contains only
  node-id endpoints; `Graph.defines` retains historical qualified aliases for
  compatibility. Source indexing refuses symlink components and empty roots.
  Terminal 1 owns compiler/LSP integration; Terminal 4 should preserve the
  existing Graph/NodeInfo fields and digest metadata when applying future
  changes.

## R2-09 — the substrate is scaled (2026-09-26)

**The problem with this module was never the graph; it was that answering any
question about it cost O(depth) syscalls per file and O(V^2) per ranking.**
Measured on this repository BEFORE the change (289,584 files):

| hotspot | before |
|---|---|
| `_has_symlink_component` | **4.97 ms/file** → 23.9 min projected at 288k files |
| source-digest pass (`_snapshot_digests`) | **did not finish inside a 50-minute budget** |
| full graph build (`CodeGraph.load_or_build`) | **1,987.5 s** (33 min) |
| per-path syscall cost on this host | `os.lstat` by path **105 us**; the same fact off an `os.scandir` entry **0.18 us**; a `WindowsPath` object **17.75 us** |

### 1. `PathSafety` — the symlink check is hoisted (the highest-leverage fix)

New public surface: `PathSafety` (`for_root`, `observe`, `observe_key`,
`observe_dir`, `observe_file`, `has_symlink_component`,
`has_symlink_component_key`, `is_safe`, `key_of`, `root`, `stats`).
`_has_symlink_component` is KEPT unchanged as the per-path fallback.

Each **directory** is classified once — from the `os.scandir` entry the walk
needed anyway — and a file's safety then costs a `set` membership test, no
syscall. The hot path is plain strings on purpose: `normcase` +
`os.path.dirname` walking, and child keys built by concatenating an
already-normalized parent key (`_child_key`), because `DirEntry.path` is a
Python-level `os.path.join` and `abspath` measured 13 us on this host.

New shared walk: `iter_source_entries(root, safety=None, *, max_bytes=...)`
yields `SourceEntry` (a class with a **lazily built `path`** — a `Path` is
constructed only by a caller that is about to read the file). It prunes
`SKIP_DIR_NAMES` and symlinked directories FROM the walk (the old code
enumerated everything and filtered afterwards) and stats each entry exactly
once through its dirent.

**Measured after:** hoisted check **5.31 us/file**; the required 300k-file
synthetic tree completes in **1.592 s** (floor 0.526 s, ratio 3.03x). That is
**~936x** faster per file. Pinned by
`tests/test_ceiling_r2_09_scale.py::test_path_safety_check_answers_by_set_lookup_not_by_syscall`
(syscalls bounded by the DIRECTORY count, machine-independently) and
`...::test_path_safety_check_over_300k_files_completes_within_budget`
(env-gated on `NEO_R209_SCALE=1`; the 30k default variant runs 2.93x the host
floor). `test_a_symlinked_file_is_still_refused_and_a_symlinked_directory_is_never_entered`
pins that the optimization did not weaken the refusal.

### 2. Stat-based freshness, with the blind spot named

New public surface: `SourceSnapshot` (`.digests`, `.stats`,
`.digest_source`, `.content_digested`, `.stat_reused`, `.blind_spot`,
`.to_dict()`, `.receipt()`), `snapshot_sources(...)`, `SourceEntry`, and the
constants `DIGEST_SOURCE_CONTENT|STAT|MIXED` + `DIGEST_SOURCE_VALUES`.

A file whose `(size, mtime_ns)` is unchanged since the stored snapshot keeps
its previous digest **without reading the file**. `digest_source` says which
path ran (`content` / `stat` / `mixed`) and `receipt()["stat_blind_spot"]`
names the exact miss class: *a content-only edit that preserves both size and
mtime_ns is not detected on the stat-reused path*. That is an optimization
with a documented blind spot, not a correctness claim.

**The blind spot is closable, and that it changes the answer is pinned.**
`CodeGraph(repo, root, *, verify_digests=None)` — keyword-only, tri-state, so
a caller who has not thought about the trade is not opted into either
behaviour by a truthy default. `verify_digests=True` forces the content pass
and restores the historical "same size + preserved mtime is a change"
guarantee at the historical cost. It is also the key-presence config hook:
**`code_graph_verify_digests` is deliberately NOT in `harness/config.py`
`DEFAULTS`** (a default is merged into every task and every eval arm).
`test_verify_digests_restores_the_content_pass_and_its_documented_blind_spot`
shows the stat pass missing the same-size/preserved-mtime edit and the forced
pass catching it.

`meta.json` gains three additive keys and keeps every historical one
byte-identical in shape: `source_stats` (`{rel: [size, mtime_ns]}`),
`digest_source`, `digest_receipt`. `CodeGraph.freshness_receipt` exposes the
last pass. The existing VEX-ARCH-03 test
(`test_graph_content_digest_rebuilds_when_mtime_is_unchanged`) still passes: it
appends text, so the SIZE changes and the file is re-read.

**Measured on this repo:** full-repo digest pass **17.50 s cold / 0.222 s
warm** (`digest_source: "stat"`, 442 reused / 0 content-digested). Before:
>50 min.

### 3. `SourceSnapshot` also removed a duplicated whole-tree walk

`_snapshot_digests` and `_snapshot_mtimes` were each their own `rglob("*")`
plus per-file safety walk, so `load_or_build` + `_save_locked` walked the
repository up to four times per build. Both are now views over ONE
`iter_source_entries` pass, and `CodeGraph` keeps one `PathSafety` for its own
lifetime (`_save_locked` no longer builds a second one inside the lock).

### 4. `_caller_id` — a 1,970-second quadratic found while measuring

**This was not in the brief; it was 99% of the build time and the brief's
"end-to-end retrieval >600 s" could not be honestly reported without it.**
`_resolve_calls` called `_caller_id` per call site, and `_caller_id` scanned
**every node of the graph for each of the three node-kind prefixes** — O(call
sites x nodes). On this repository that was 1,970 s of the 1,987.5 s build.

Fix: `_qualified_index(graph)` builds `info.qualified -> [node_id, ...]` in
`graph.nodes` insertion order ONCE per build, and `_caller_id` takes it as a
keyword-only `index`. The candidate list, its order, and the `caller_file`
preference are unchanged; `_caller_id_reference` is the old linear scan kept as
a **test oracle**, and
`test_indexed_caller_lookup_agrees_with_the_linear_scan_on_every_call_site`
requires an exact match for every call site of a real graph.

**Measured on this repo:** full build **1,987.5 s → 28.4 s** (70x), same node
and edge counts.

### 5. Not implemented / honest notes

- **A half-built index is never persisted.** `build()` has no deadline, so a
  cold build cannot be interrupted mid-parse. `harness.retrieval` therefore
  bounds the stages it owns and reports the index load's measured cost rather
  than pretending to cut it. A `build(deadline_s=...)` that produced a
  partial graph would need `Graph.truncated` + a refuse-to-persist rule first;
  that is not built.
- **The stat path's blind spot is real** and stated in three places
  (`SourceSnapshot.blind_spot`, `receipt()["stat_blind_spot"]`, and the
  `CodeGraph` docstring). `verify_digests=True` closes it.
- `PathSafety` is **not thread-safe** (one per builder/CodeGraph instance, as
  each owns its own lock). A shared instance across threads would need a lock.
- A 300k-file proof run costs ~8 minutes on this host (350 s of it building
  the fixture with hard links), so the full-scale variant is env-gated. The
  always-on 30k variant asserts the same invariants.
- **No live-provider lane and no Docker lane were run.** Every measurement is
  host-side and offline. The measurements were taken on a tree other terminals
  were actively editing, so the per-stage numbers carry rebuild noise; the
  ratios (per-file cost, syscall counts, quadratic vs linear) do not.

### 6. Cross-terminal requests

- **`harness/retrieval.py::_SKIP_DIRS` is the single largest remaining cost in
  the retrieval path, and widening it is NOT mine to decide.** Measured on
  this repo: the same scandir walk with the **code-graph** skip set opens
  **155 directories in 0.42 s**; with retrieval's `_SKIP_DIRS` it leaves
  **124,774 directories and takes 144.6 s** (1.16 ms per `os.scandir` open on
  this host). The unpruned trees here are `site-v1-backup`, `logs`,
  `probe_logs`, `Temp`, `pip`, `Microsoft`, `.shots`, `graphify-out`. Adding
  `site`/`logs`/`env`-class entries would cut end-to-end retrieval by ~345x,
  but it changes **which files can be retrieved**, which is a retrieval-quality
  decision. Requesting it from the retrieval-semantics owner with the numbers
  above rather than taking it.
- **`memory.code_graph` is imported directly by `harness.retrieval`** for
  `PathSafety` (via a guarded `_path_safety_for`, which returns None and
  falls back to the per-file check if the import fails). It is pure stdlib
  with no tree-sitter dependency at call time, so this does not make
  retrieval depend on the optional grammars — but if the contract owner wants
  that import to go through `harness.deps`, this is the call site.

## Ceiling Terminal 05 — memory capture reaches the daily agent (2026-09-26)

The memory layer's WRITE side was already strong: `authorize_memory_write`
fails closed on provenance, authority, and secrets, and
`DecisionStore.record` refuses unsafe text. What was missing was an agent
that could actually write through it, and a dedupe that spans sessions. Both
are now closed from the memory layer's perspective; the agent-side wiring is
documented in `harness/AGENTS.md` and the contracts in `INTERFACES.md`.

### `memory_record` — the agent's write path

`memory_record` is a canonical catalog tool with `requires_approval=True`, so
approval is required BEFORE `memory_record_enabled` is consulted. A stored row
outlives the session and is read back by later ones, which is why writing one
is an operator-visible effect and not a read.

Every write carries provenance (`memory_provenance`): repo, session, run, task,
source, model, provider, epoch `timestamp`, `iso_timestamp`, and an
`explicit` marker. Without that block the shared gate refuses the write for any
non-operator actor.

**Deduplication had to live above this store.** `DecisionStore.record(
dedupe=True)` is keyed on `(task_id, repo_path, text)` — scoped to ONE task.
An MCP client and an agent that record the same convention would have produced
two rows. The dedupe check is therefore repository- and category-scoped over
normalized text, and a duplicate reports the **first record's id** instead of
writing. That id is the reuse proof: session 2 recording the same convention
gets session 1's row back.

**Claims are downgraded, not deleted.** Text asserting an unverified outcome
("all tests pass now", "this is definitely correct") is stored as
`category="observation"` with `claim_downgraded` set. A later session reads it
as a report, not a settled fact. Text REPORTING an outcome
("`python -m pytest tests/test_x.py` failed with ImportError") is evidence and
keeps its category.

**Secrets and authority claims never land.** A credential-shaped row is
refused with a reason that says so (`secret: True`); an authority-claim row is
quarantined. The receipt distinguishes the two so a refusal is diagnosable
rather than a generic rejection.

### `mcp_server.record_decision` — additive, same five-tool surface

`record_decision` gained optional `session_id`, `model`, and `dedupe=True`
parameters, and the provenance block now carries the capture `timestamp`. The
dedupe pre-check lives in `mcp_server._find_duplicate` rather than in the
store, because the store cannot deduplicate without a task id. `_find_duplicate`
matches on normalized text within the same category and repository (the same
sentence as a convention and as a gotcha stays two records) and tolerates a
store whose `search` does not accept the optional `repo_path` keyword.

**The tool count is deliberately unchanged at five.** `tests/test_mcp_server.py`
and `tests/test_memory_mcp_release.py` pin the exact five-tool surface, and
those are another terminal's tests; adding a sixth `memory_record` MCP tool
would have broken them. The provenance requirement is met through additive
parameters on the existing tool instead.

### Known limits and blocked lanes (honest)

- **`tests/test_memory_mcp_release.py::test_mcp_decision_tools_close_their_stores`
  fails, PRE-EXISTING and not caused by this round.** Its in-test `FakeStore`
  declares `record(self, _text, category="general", source="manual")`, and the
  historical `record_decision` already passed `provenance=` to it before this
  round. **Proven, not assumed:** calling that exact pre-existing kwarg set
  against that exact `FakeStore` signature raises
  `TypeError: ... got an unexpected keyword argument 'provenance'`. This round
  restored the historical two-branch call shape byte-for-byte (the store call
  keeps exactly the kwargs it always had) and added the dedupe as a PRE-CHECK
  rather than as a new store kwarg. The fix belongs in that test's `FakeStore`
  (the real `DecisionStore.record` does accept `provenance`, and removing it
  would break the memory-write security contract) — it is that terminal's file.
- **No labeled hallucinated-symbol sample was measured**, so no
  hallucinated-symbol rate is claimed.
- **No cross-session reuse measurement over a real corpus.** The reuse path is
  proven in isolation (two independent `KnowledgeContext` instances against
  one store) rather than across production runs.
- **No live-provider lane was run**; no credential was inspected or retained.

## AGT-09 - `StagedSnapshotStore` (2026-09-28)

`memory/checkpoints.py` gained a SECOND, deliberately different mechanism beside
`CheckpointManager`. The harness half and the full test evidence are in
`harness/AGENTS.md`; this section is what the memory owner needs.

### 1. Why it is not another `CheckpointManager` call

| | `CheckpointManager` (existing) | `StagedSnapshotStore` (this round) |
|---|---|---|
| model | a shadow-git snapshot you POP | a content-addressed store you ask for a RANGE of |
| idempotent | no - a new id every time | yes - the id IS a content digest, so an unchanged re-capture writes nothing AND journals nothing |
| scriptable | no - an in-process call | yes - a private directory plus an append-only journal; a second process sees the same staged range |
| granularity | whole checkpoint | `files` / `conversation` / `both` |

A checkpoint stack cannot answer "rewind the last three turns of the code but
keep what we said", which is the question a user actually asks.

### 2. Layout, and why there are TWO authorities

```
<log_root>/_undo/<session_id>/
  objects/<aa>/<sha256>     content-addressed bytes (one copy per content)
  snapshots/<snap-id>.json  one manifest per snapshot id
  receipts/<id>.json        one written revert receipt
  journal.jsonl             APPEND-ONLY: the authority for which turns exist
  staged.json               the authority for the CURRENTLY STAGED range
```

Two files rather than one, deliberately: the journal is durable history and
must stay append-only, while the staged range is a single mutable pointer whose
whole job is to be read, widened, or replaced. Reads tolerate a torn journal
tail; a `staged.json` belonging to a different repository is IGNORED rather than
honoured.

`_undo` was added to `_IGNORED_DIRS`, so a shadow-Git `CheckpointManager` can
never capture the undo store into its own snapshot - including the
in-repository-log-root case, which
`TestStoreProperties::test_the_store_lives_outside_the_repository` pins.

### 3. The rule that matters most

**"The file differs from the pre-image" is NOT a concurrent user edit.** A
revert is supposed to overwrite the change its own turn made, so refusing on
that difference refuses every ORDINARY revert - which is exactly what the first
implementation did, and the test suite caught it. The rule that is correct:

- a step captures a path's pre-image ONCE per turn and its post-image after
  EVERY mutation, so every content the run produced is RECORDED
  (`_accounted_hashes`, and `_accounted_conversation` for the same reason - a
  conversation grows as the run talks and a `conversation` revert is supposed to
  rewind that growth);
- a current hash that appears NOWHERE in the staged range is refused;
- `force=True` is possible and records every overwritten user edit under
  `overwritten_user_edits`, with the paths rendered.

The consequence for `harness/editor.py` is that its hooks capture AFTER EVERY
mutation, not once per turn. An optimisation that captured only per turn would
make the second edit of a turn look like a concurrent user edit.

### 4. Honesty details that are not obvious

- `verified` is `bool(checked_paths) and not refused and all(...)`. `all([])` is
  `True`, so without the first term a receipt that restored nothing and refused
  everything CLAIMED it was verified. `checked_paths` counts restored +
  unchanged.
- A refused path leaves the staged range PENDING - only a fully clean revert
  clears `staged.json` - so a user who hits a conflict does not lose the range
  they built.
- Exclusions travel in the receipt. `gitignored` is asked of
  `git check-ignore --stdin -z` in ONE batched call (a subprocess per file is a
  subprocess per file), and `ignore_authority` records whether git or the
  built-in directory set answered, because "nothing was ignored" must be
  distinguishable from "we could not ask".
- The out-of-scope filter runs BEFORE the existence check, so an out-of-scope
  CREATE is filtered too; filtering only the existing files let one through.
- The scope genuinely gates its axis. A `conversation` revert writes no file
  and the receipt says `restored: []` - a granularity that also reverted code
  would be a granularity that is a lie.
- `RESTORE_SCOPES` is the product's EXISTING rewind vocabulary
  (`harness.agent_kernel.context.rewind_run`). `memory` may not import `harness`
  (the dependency direction is cli -> memory), so equality is pinned by a test
  rather than by an import - the same technique the edit-refusal slugs use.

### 5. Known limits, stated plainly

- **No object-store GC.** `max_turns` prunes journal rows; the objects stay. A
  receipt may still name one, so deleting them automatically would be unsafe.
  `prune()` is the seam.
- **The staged range is per `session_id`.** Two sessions in one repository hold
  two independent ranges and neither can widen the other.
- **`turn_pruned` rows are written and consumed by nothing.**
- `_restore_conversation` writes into `<log_root>/_conversations/`, which
  `cli/session.py` owns, and only for the `conversation` / `both` scopes. It
  hashes first and refuses on an unaccounted change, exactly like the files
  axis.
- `stage()` never silently re-granularises a range that is already staged:
  widening must not throw away a scope the user deliberately chose, so
  changing it is its own operation (`set_scope`).

### 6. Verification

`tests/test_agt_09_staged_undo.py` -> **68 passed, 1 skipped** (a Windows
symlink-privilege case, i.e. BLOCKED coverage, not a pass). The full neighbour
sweep is listed in the `INTERFACES.md` Change Log entry and in
`harness/AGENTS.md`. `python -m evals.run --check` -> 14/14 CLEAN. **No Docker
lane and no live-provider lane were run**, and neither is claimed.

## T5 P1/W1 - `_caller_id` is indexed on BOTH branches, and the pin that missed it (2026-10-02)

**File:** `memory/code_graph.py`. **Tests:** NEW
`tests/test_code_graph_caller_index.py` (9 tests).

### The defect

`_resolve_calls` resolves each call site's enclosing symbol through
`_caller_id`. The R2-09 optimisation indexed only the **symbol-level** branch
and left this one scanning every node:

```python
if caller_qualified == module:
    if caller_file:
        for node_id, info in graph.nodes.items():      # <-- O(nodes), per call site
            if node_id.startswith("module:") and info.file == caller_file:
                return node_id
```

With one module-level call site per file times 20,065 nodes, that is
O(files x nodes). Measured on this repository, 2026-10-02, from rung #9 of the
Trust Ladder:

| | before | after |
|---|---|---|
| `_caller_id` | **134,069 calls, 34.7 s** | **1.1 s** |
| `str.startswith` calls it drove | **55,436,109** | not in the top phases |
| forced full graph rebuild | 88.7 s | **24.6 s** |
| `retrieve_context` cold, profiled | 93.2 s | 52.0 s |

### The fix

`CallerIndex` (`__slots__`, built once per build) holds two maps that
collapse all three scans into dict lookups:

- `by_qualified: Dict[str, Dict[str, List[str]]]` — outer key the node-id
  prefix, inner key the qualified name, list in `graph.nodes` **insertion
  order**.
- `module_by_file: Dict[str, str]` — the first `module:` node per file. Built
  with `setdefault`, because the oracle returns the FIRST match and a
  last-wins index would be faster and wrong.

`_qualified_index(graph)` now **returns a `CallerIndex`** rather than a plain
dict. A caller passing the old dict shape still works; it just loses the
module-branch speedup. `NODE_KIND_PREFIXES` is pinned because a reorder
changes which candidate wins, and therefore which call edges exist.

### The part worth copying: why the existing pin passed

`tests/test_ceiling_r2_09_scale.py:746` pins `_caller_id` against
`_caller_id_reference` - the correct test, on a **4-file, 8-node fixture
comparing 4 call sites**. An O(nodes)-per-call-site scan is *free* at that
size.

`tests/test_code_graph_caller_index.py` keeps the same oracle and raises the
fixture to **>= 40 nodes and >= 12 call sites**, asserts the module-level
branch is actually compared (`module_level >= 1`), and adds a **scaling
test**: two graphs 8x apart, and the per-call-site module-branch cost must not
grow proportionally. A correctness test cannot catch a complexity regression;
only a scaling test can, and the bound is deliberately loose (12x) so it
asserts the ORDER OF MAGNITUDE rather than a figure that would flake in a
blocking lane.

`test_the_fixture_is_large_enough_for_the_defect_class_to_be_visible` is the
non-vacuity gate, and it is the assertion that would have caught the original
miss.

### What is still slow, and why it is not this fix

The residual ~34 s of a cold `retrieve_context` is a whole-repository
tree-sitter reindex (579 files / 20,065 nodes) plus ~23 s serialising the
persisted graph to JSON. `memory/code_graph.py` has **no incremental index** -
its own docstring says so - so one source edit forces a full rebuild. That is a
P3a/P4-scale design decision, not a P1 speedup, and it is recorded as
`not_implemented` rather than papered over.

Two constraints that stopped the obvious fix, so nobody re-attempts it: the
graph digest is a **compatibility contract** with every stored `graph.json`,
and the serialisation options differ between `_atomic_write_json`
(`indent=1`) and `_graph_digest` (`sort_keys=True, separators=...`), so they
cannot share one pass without changing the digest value and invalidating
every persisted graph.
