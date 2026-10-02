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
  - `query_decisions(query="", repo_path="")` — ranked search of decision
    memory; every call first lazily ingests new `logs/*/state.json`
    decisions. `repo_path` optionally enforces canonical repo scoping.
  - `record_decision(text, category="general", repo_path="") -> str` —
    append a fact, optionally scoped to one repository.
  - `task_status(task_id)` — Boundary 4 state.json summary (spec item 31).
  - `list_repos()` — indexed repos + file counts.
- Decision DB + graph indexes resolve through `memory/paths.py` (env
  overridable, isolated in tests via `HARNESS_HOME`).
- Graph objects are cached per repo in-process and refreshed against the
  persisted mtime/digest snapshot on every query; server-side state is
  thread-safe.

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

## Product round 2026-09-24 — real external MCP verification

- The five live tools remain `query_structure`, `query_decisions`,
  `record_decision`, `task_status`, and `list_repos`. Decision tools now
  accept an optional `repo_path` for strict repository-scoped external
  clients; the no-argument form remains global and backward compatible.
  Terminal 4 reconciled the additive signatures in `INTERFACES.md`.
- `task_status` resolves the final state file through `safe_state_file`, so
  traversal-shaped ids and state-file symlink escapes are both refused.
  Decision text is redacted before MCP output/trace, and secret-bearing
  records are rejected at the store boundary.
- `memory.mcp_client` now guarantees transport/session cleanup with an
  async context manager and forwards only safe child environment variables;
  provider keys/tokens are not inherited. The real stdio test initializes,
  lists exactly five tools, calls all five, and closes the session.
- Verification: MCP-focused run returned exit 0, 63 passed, 1 skipped;
  the skip is the Windows symlink privilege case. The required eight-file
  run returned exit 0, 149 passed, 1 skipped; report path is recorded in
  `memory/AGENTS.md`. No live provider lane was run because credentials
  were intentionally isolated and blank.
- Integration request: Prompt 1/2 should pass `repo_path` and render the
  MCP receipt in the agent-loop context; no changes were made to their
  owned loop or TUI files.

## Product round security follow-up 2026-09-24

- `task_status` now redacts plan, completion, remaining, file, decision, and
  state task-id fields before rendering or tracing; `list_repos` and
  structural query output are redacted as well. The last-repo pointer is
  written through a flushed sibling temp file and atomic replace.
- `memory.mcp_client` now filters inherited environments through a narrow
  allowlist, while preserving explicitly caller-supplied authorization
  variables such as `GITHUB_TOKEN` and `SERVICE_API_KEY`.
- MCP-focused verification after the changes: 64 passed, 1 skipped, exit 0.
  The plain required command is blocked by the unavailable Docker daemon;
  the documented `HARNESS_EXEC_SKIP_DOCKER=1` lane is 157 passed, 3 Windows
  symlink skips, exit 0, with no MCP test failure.
- Terminal 4 release reconciliation re-ran the expanded dashboard/memory/
  MCP/adversarial selection: exit 0, 116 passed, 4 skipped. The skips are
  Windows symlink-privilege cases and are not counted as passes. Targeted
  Ruff is clean.

## VEX-ARCH-04 lifecycle and health (2026-09-25)

- The server still exposes exactly five MCP tools. `server_health()`,
  `server_lifecycle_events()`, and the typed `ServerLifecycleEvent` are local
  non-tool projections; startup, tool-call, and shutdown observations are
  bounded and do not contain tool payloads.
- The client half now owns a reusable `McpClient` stdio context with typed
  spawn/initialize/list/call/shutdown/failure events, bounded operation
  timeouts, health metadata, and the historical synchronous wrappers.
- Real verification covers one connection performing initialize, list, and
  call, followed by observable client shutdown. The CLI connector wrapper is a
  cross-owner handoff and still uses its older daemon-thread health probe.
- Required six-file verification: 124 passed, one Windows symlink skip. No
  Docker or live-provider lane was selected; neither is reported as passed.

### Final audit additions

- `McpClient.connect` now cleans partial transport/session setup after failed
  initialization, and the synchronous compatibility facade cancels timed-out
  worker tasks before closing their event loops.
- Final required verification is `132 passed, 1 skipped`; the MCP-focused
  client/server tests and scoped Ruff/compile checks are green.

## VEX-CEILING-12 - namespaced tool ids, least privilege, hash pinning (2026-09-26)

**NEW mcp_server/namespace.py.** An external MCP server is the least-trusted
thing the agent talks to: a tool whose schema the operator never wrote, returning
content the operator never saw. This module is the boundary between "an MCP
server said so" and "the agent may act on it". Four properties, each with a
regression test in 	ests/test_ceiling12_hooks.py.

### 1. Namespaced tool identifiers compatible with common clients

Every tool is addressed as `mcp__<server>__<tool>` - the convention Claude
Code, Cursor, and the other common MCP clients already parse, so a client
routes a name without a lookup table. Segments are normalized to
`[A-Za-z0-9._-]`; a disallowed run becomes `-`; runs of `__` collapse to a
single `_` and a dash next to an underscore collapses to `-`, so a hostile
label (`evil/__server`) can never inject the separator. Every produced name
round-trips: `parse_namespaced_tool_name` returns the exact `(server, tool)`
pair. An unparseable name is a refusal with **no** best-effort parse - a name
the agent cannot attribute to a server is a name it may not call.

### 2. Per-server / per-tool least privilege

`MCPToolPolicy` maps a server to the set of tools that server may expose.
Everything is closed by default: an unlisted server is denied, an unlisted tool
within an allowed server is denied, and a tool that declares **no** side-effect
class is treated as mutating. Three independent gates run in `authorize` -
server configured, tool exposed, declared side-effect class within the session
ceiling (`read < search < network < mutation < destructive`). A `"*"` tool
value is an explicit operator decision, distinct from the default, and
`describe()` reports it as such. `visible()` returns only the permitted
tools, so **the catalog a client renders is the catalog it may call** - the two
are the same list, not two that can drift.

### 3. Tool definition hashes pinned at approval

`tool_definition_digest` is a domain-separated (`neo/mcp-tool/v1`) SHA-256
over canonical `(server, tool, description, inputSchema)`. The schema is
key-sorted, depth-bounded, and node-budgeted, so a reordered schema hashes
identically while a changed one does not. `ToolPinSet` records the digests an
approval was granted against; `verify()` re-hashes the live catalog
immediately before the call and raises `MCPToolDefinitionChanged` naming the
tool and BOTH digests. A pinned tool that vanished from the catalog raises the
same error - both are "the thing you approved is not the thing that would
run". `ToolPinSet.as_dict()` is shaped for an approval ticket.

`ToolPinSet.authorize(policy, catalog, name)` runs least privilege FIRST, so
an unauthorized tool is refused as unauthorized rather than as a pin mismatch.
The ordering is load-bearing and pinned by a test.

### 4. Server responses are untrusted content

`review_tool_result(payload, server=..., tool=...)` routes the result through
`shared.security.review_untrusted_source(source="mcp")` and returns
`(usable_text, review)`. A quarantined result is replaced with an explicit
refusal naming the source and the hostile text is **not forwarded**; a merely
flagged result is returned with its taint banner. `UntrustedReview.as_dict()`
carries no payload, so a receipt can prove the decision without republishing the
content.

### Server-side additions (mcp_server/server.py, additive only)

- `_SERVER_TOOL_SIDE_EFFECTS` classifies the five tools. `record_decision`
  is the only `mutation`; the three readers/lister are `read`/`search`. A
  tool absent from the map falls to `mutation`, so adding a tool without
  classifying it fails toward the more restrictive side.
- `server_name()`, `server_tool_definitions()`,
  `server_tool_catalog(policy=None)`, `server_tool_pins()`.

**No tool signature changed and no tool was added or removed.** The five
Boundary-5 tools and the six-key server_health() projection are byte-identical;
this is a read-only catalog layer beside them.

### Verification

	ests/test_ceiling12_hooks.py::test_required_5_mcp_tool_names_are_namespaced_and_hash_pinned
plus the least-privilege, digest-stability, untrusted-result, and pin-gate-order
tests in the same file. 	ests/test_mcp_server.py + 	ests/test_mcp_adversarial.py
re-run green. No live provider lane was run; none is required by this module.

## R2-16 - the namespace layer is no longer dead code (2026-09-26)

The VEX-CEILING-12 section above ends with "Not wired, by design and recorded
as a handoff". That is now closed for the call path this terminal owns, and the
test that keeps it closed is named after the behaviour so it cannot be quietly
dropped.

### What "wired" means concretely

`mcp_server/namespace.py` was NOT edited this round. It is now reached from
`cli/connectors.py::call_tool`, the pre-call boundary for every external MCP
tool call the product makes (`neo mcp call`, `/mcp`):

1. the `PreToolUse` user-hook gate is evaluated BEFORE any server process is
   spawned (a blocking or fail-closed verdict refuses the call);
2. the LIVE catalog is fetched;
3. `MCPToolPolicy.authorize` runs - least privilege FIRST, so an unauthorized
   tool is refused AS UNAUTHORIZED rather than as a pin mismatch;
4. the connector's own declaration gates the tool further (side-effect
   ceiling, `write`, `network` host allowlist);
5. `ToolPinSet.verify` re-hashes the live catalog against the digests the
   approval was granted against - and a pinned tool that vanished raises the
   same `MCPToolDefinitionChanged`;
6. the call is dispatched;
7. `review_tool_result` routes the RESULT through
   `shared.security.review_untrusted_source(source="mcp")`, so a quarantined
   answer is replaced with an explicit refusal and the hostile text is not
   forwarded.

`list_tools` returns the namespaced `mcp__<server>__<tool>` catalog. When the
connector carries a permission declaration the RENDERED CATALOG IS THE CALLABLE
CATALOG - `policy.visible()` is what produces the list and the refused tools
are reported separately under `blocked`, so the two cannot drift.

### The regression test that cannot rot

`tests/test_r2_16_extension_ops.py::test_the_mcp_namespace_layer_is_reached_by
_a_real_call` asserts one of two things and says which: the layer is REACHED by
a real call, or it is ABSENT from the tree. There is no third state. "Real"
means a real `python -m mcp_server` subprocess over stdio, spawned by the same
client the product uses, with the returned `ConnectorReceipt` naming the
namespaced identifier, the 64-hex definition digest, and the untrusted review.

`test_the_namespace_layer_refuses_an_undeclared_tool_on_a_real_server` drives
the SAME real subprocess and proves the layer is a gate and not a pretty
printer: a tool the operator never named cannot be called, and the refusal
names the namespaced id.

### One honest limitation, surfaced not hidden

`memory/mcp_client.py::list_mcp_tools` normalizes a listed tool to
`{name, description}` - it drops the `inputSchema` and any declared
side-effect class. A tool that declares NO class therefore lands on
`DEFAULT_SIDE_EFFECT_CLASS` (`mutation`), which is the conservative direction,
but it also means the per-tool `networkDomains` a server publishes in its
schema is invisible to the host gate, so every network-capable tool is treated
as undeclared and REFUSED. Fixing it means preserving the full descriptor in
`memory/mcp_client`, another owners file; the request is filed in
`cli/AGENTS.md`. Until it lands, the two tests that exercise a class-carrying
catalog stub the LIST call explicitly and say so in their docstrings.

### Not implemented / honest

- No MCP tool was added, removed, renamed, or re-signed. The five Boundary-5
  tools and the six-key `server_health()` projection are byte-identical.
  `SIDE_EFFECT_CLASSES` and `DEFAULT_SIDE_EFFECT_CLASS` are still not in
  `namespace.__all__` - pre-existing, and deliberately left alone to keep this
  rounds diff to wiring.
- The kernel and the legacy agent loops still call external MCP tools through
  `memory.mcp_client` / `harness.agent_loop` directly, not through
  `cli/connectors.call_tool`. Those are other owners files; the request is
  filed in `cli/AGENTS.md`. Until they route through this gate the namespace
  layer protects the CLI connector surface, not every MCP call in the product.
- No live MCP server other than this repository's own `python -m mcp_server`
  was contacted, and no live provider lane was run. Neither is claimed.

## VEX-CS-07 — deferred schemas, server-contributed commands, the run budget

**Files this round created and edited:** NEW `mcp_server/tool_pinning.py`;
`cli/connectors.py` (the consumer); NEW `tests/test_mcp_command.py`;
`mcp_server/AGENTS.md`. **`mcp_server/namespace.py` was READ and NOT edited** —
the gate is done and this round wired things *beside* it, never through it.
`mcp_server/server.py`, `memory/mcp_client.py`, `cli/tui.py` and `cli/commands.py`
were not opened for edit. **No Boundary-5 tool was added, removed, renamed or
re-signed** — the five tools and the six-key `server_health()` projection are
byte-identical, and `tests/test_mcp_server.py` is green.

Machine-readable handoff: `logs/command-surface/terminal-07.json`. Measurements:
`logs/command-surface/terminal-07-measure.json`.

### 0. What `namespace.py` owns and what this file does NOT touch

`namespace.py` is the SECURITY boundary: namespaced identifiers, least
privilege, approval-time digests, untrusted results. It is read-only over what a
server published and it never decides *what the model is shown*. That is the
line this file sits on: it decides what is shown, what a schema costs, what a
prompt becomes, and how many calls a run may make — and it refuses to re-derive
a single digest.

### 1. Deferred schemas — measured, with the honest caveat

`deferred_catalog` returns `{name, description, side_effect_class,
definition_digest}` and **no `inputSchema`**. `load_schema` fetches the schema
for exactly the tool the caller named. `measure_schema_deferral` reports the
before/after token receipt with its divisor, because a "saves tokens" claim with
no number in it is a slogan.

| arm | tools | eager | deferred | saved (no request) | saved (ONE schema requested) |
|---|---:|---:|---:|---:|---:|
| this repo's `python -m mcp_server` | 5 | 500 | 204 | **59.2 %** (296 tok) | **15.8 %** (79 tok) |
| generated 40-tool server | 40 | 6 870 | 1 650 | **76.0 %** (5 220 tok) | **74.9 %** (5 144 tok) |

Divisor 4, a characters/token heuristic, **not a tokenizer** — and the honest
caveat is the second column: on the real five-tool server, asking for ONE schema
takes the saving from 59 % to 16 %, because one of its five schemas is 59 % of
the catalog. **The feature pays off as the catalog grows.** On a five-tool server
it mostly pays when the model asks for nothing. That is measured, not argued, and
it is why the receipt carries both figures.

Divisor sweep (3 / 4 / 5) on the no-request arm: **0.5916 / 0.5920 / 0.5925**
(real) and **0.7598** at all three (fixture). The ratio is stable because both
arms divide by the same divisor — which is the property that makes it worth
quoting at all.

**A negative saving was measured first, and that is why the row forms are two.**
The first draft shipped one merged row carrying `definition_digest` on every
tool. The digest is 64 hex characters — 16 tokens a tool at this divisor — which
cost MORE than the entire `inputSchema` payload the deferral removed, so the
small-catalog arm measured **-69 %**. `ToolSummary` therefore has two
projections: `as_dict()` (the shipped receipt form, digest included, because
several suites assert the digest) and `as_model_dict()` (name + description +
class). `measure_schema_deferral` reports **both** savings. A receipt that
quotes only the flattering one is a receipt shaped to sell the feature.

### 2. Server-contributed commands

`prompt_command_name(server, prompt)` returns `/mcp__<server>__<prompt>` by
calling the **same** `namespaced_tool_name` a tool name goes through, so a
hostile prompt name is normalized identically and cannot inject the separator.
`prompt_commands` skips a prompt with no name rather than rendering a command
nobody can invoke.

The prefix was `"/mcp__"` in the first draft and produced
`/mcp__mcp__memory__summarise` — exactly the "two spellings of one thing" the
namespace layer exists to prevent. It is `"/"` in front of the existing
namespace, and that is what ships.

Discovery is over the real protocol (`prompts/list` through
`cli.connectors.raw_catalog`), not over a config file. A server with no prompt
capability is a FACT (`ok=True`, empty rows, reason recorded), not a failed
probe: rendering "no prompts" as an error would make a healthy server look
broken.

### 3. The per-run budget

`ToolBudget` records `tools_exposed`, `calls_made` and
`projected_context_tokens`, plus the per-server split (`tools_by_server`,
`tokens_by_server`, `calls_by_server`, `share_by_server`) — because "one server
owns the window" is not answerable from a total.

It is a **gate**, not a dashboard: `record_call` refuses the call that would
cross a declared ceiling and does **not** count a refused call, so a receipt can
never report its own refusal as consumption. `would_allow` is pure, so a caller
may ask before committing. Measured receipt cost **0.235 ms** per
exposure + call + receipt.

`budget_from_config` reads `mcp_max_tools_exposed`, `mcp_max_tool_calls` and
`mcp_chars_per_token` **by key presence** and none of them is in
`harness/config.py::DEFAULTS`, because a DEFAULTS value merges into every task
and every eval arm and "how many MCP calls may this run make" is a property of a
connector session. A present-but-unusable value is REPORTED in `notes` and the
declared default is used, so a typo cannot produce a budget nobody asked for.
Shipped ceilings: **64 tools exposed, 32 calls, divisor 4**.

`record_exposure` is idempotent per server. A budget that double-counted a
re-list would report a server as more dominant than it is, which is the same
class of dishonesty as reporting the wrong total.

### 4. Strict mode: the surfaced default is unchanged

`strict_undeclared_reason` returns `None` for the shipped default — an undeclared
connector runs with `enforced: false` and a reason that says the call was NOT
gated, exactly as R2-16 left it. `mcp_require_declared_connector` is an OPT-IN,
read by **key presence**: absent, `False`, `"false"`, `"no"`, `[]` and `0` all
mean not-strict, and `True` / `"true"` / `"yes"` / `"require"` / `"strict"` / `1`
all mean strict. A declared connector is unaffected, or the opt-in would be a
global kill switch. The refusal names `neo mcp permissions <label> --tool ...`.

### 5. The one thing this round did NOT change, on purpose

**`memory/mcp_client.list_mcp_tools` still normalizes a tool to
`{name, description}`, so every definition DIGEST is still computed over an
empty schema.** Making the raw descriptor the catalog source of record would
invalidate every recorded pin and un-gate every test that injects a catalog, so
this round reads the raw catalog as an ENRICHMENT beside the client read and
never re-derives a digest from it. One digest per tool, always.

`cli.connectors._catalog_read` is the one place that decides this, and both
`list_tools` and `call_tool` go through it so a digest recorded from the listing
and a digest re-hashed at the call can never be two different answers. The
receipt says which source answered (`schema_source`), and
`deferral.digest_source` says the digests came from the client descriptors in
BOTH arms.

**Handoff to the memory owner — filed, not applied.** `memory/mcp_client.py`:
keep the existing `{"ok","tools","error"}` envelope and ADD `raw_tools` (the SDK
`ListToolsResult` items verbatim: `{name, description, inputSchema,
annotations}`) plus `prompts` (`{name, description, arguments:[{name,
description, required}]}`), and add `list_mcp_prompts`. Two consumers are already
waiting for it: `cli.connectors._raw_probe` and
`mcp_server.tool_pinning.mcp_client_shaped_descriptors`. Until it lands the
per-tool `networkDomains` stay invisible to the host gate, so a network-capable
tool is still treated as undeclared and refused — the gap this module's own
`measure_schema_deferral` works around rather than papers over.

### 6. Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)

- **NEW `tests/test_mcp_command.py` -> 64 passed** (2 m 53 s). Host-only: no
  Docker, no provider, no network, no credential. Real processes inside it: a
  generated stdio server publishing two prompts and two tools, a generated server
  that HANGS on purpose, and this repository's own `python -m mcp_server`.
- **REQUIRED lane 1** `test_mcp_server.py test_mcp_adversarial.py
  test_cli_connectors.py test_r2_16_extension_ops.py` -> **109 passed, 1 skipped,
  2 failed** (~116 s). The skip is the Windows symlink-privilege case and is not
  counted as a pass. Both reds are attributed in §7.
- **Neighbour lane** `test_cli_slash2 test_cli_terminal_parity
  test_cli_command_system test_ceiling12_hooks test_mcp_client
  test_memory_mcp_release test_extensions` -> **261 passed, 3 skipped**.
- `python -m ruff check` clean on every file this round created or edited;
  `python -m compileall -q` exit 0 on all five.
- **Not run and not claimed:** no Docker lane, no live-provider lane (no
  credential inspected, printed or retained), no `python -m evals.run` matrix
  (this round changed no prompt), no full-suite run, no real attached-PTY
  campaign.

### 7. Two reds in the required lane, attributed and NOT counted as passes

1. `tests/test_cli_connectors.py::test_duplicate_plugin_labels_keep_winning_source`
   — `cli/plugin_runtime.py:725` requires BOTH `name` and `version` in
   `plugin.json` and the test's own fixture has no `version`. Another terminal's
   manifest validation; this round edits no plugin code.
2. `tests/test_r2_16_extension_ops.py::test_neo_hooks_run_reports_the_fail_policy_it_used`
   — `neo hooks run --json` publishes `{command, exit_code, lines, payload,
   status}` and no `allowed` key. Measured by running it directly.
   `cli/main.py` is not this round's file.

Neither function is reachable from this round's diff. **No assertion was
weakened and no timeout was added to make anything green.**

One transient worth recording: `extensions/user_hooks.py` took **twelve** of the
required lane's tests red mid-session with
`TypeError: Attempted to reuse key: '_name'` at `HookEvent` class construction
(and a second, deeper MRO defect behind it). It is another module's untracked
in-flight file; the owning terminal fixed it during this round and those tests
are green now. It was NOT edited here — the standing rule is that a
cross-terminal breakage is reported with the measurement, not patched from a
module that does not own it.

### 8. Handoff to 01 (the REPL/TUI mount — filed, not applied)

`cli/interactive.py::mcp_subcommand` is Terminal 01's. The ONE implementation of
the nine `/mcp` verbs is now **`cli.connectors.mcp_command(verb, argument,
repo_path=..., config=...) -> {ok, verb, lines, payload}`**, and that branch
carries a second copy of five of them plus four honest refusals for verbs that
now exist. Replace its body with:

```python
from cli import connectors as _conn

_result = _conn.mcp_command(verb, rest, repo_path=repo_path)
return _receipt(
    name,
    bool(_result["ok"]),
    _result["lines"],
    payload=_result["payload"],
)
```

`lines` is always non-empty, so no branch has to invent an empty-state sentence,
and the no-argument `list` case keeps `_render_mcp` as its renderer so the
historical `/mcp` and `/mcp <label>` lines stay byte-identical. Exact snippets,
plus the `cli/tui.py` and `cli/main.py` equivalents, are in
`logs/command-surface/terminal-07.json` under `handoff_to_01`. Not applied:
`cli/interactive.py` is not this round's declared ownership.

### 9. Handoff to 06 (the plugin server seam — agree, do not build twice)

`cli/plugins.py` is Terminal 06's. **This round's side of the seam is
`cli.connectors.raw_catalog(ref, ...) / _raw_probe(entry, ...)`: one bounded
spawn, one read, no pooling, no cache, no handle.** What is NOT provided and
would have to be: a reusable session handle, a cache with an eviction policy,
and the start/stop trigger. A plugin whose `mcp_servers` entry is enabled should
start one and hold it; disabled or uninstalled should stop it. **Neither side is
implemented** — this round does not touch plugin lifecycle, and Terminal 06
should not add a second raw-MCP read beside `_raw_probe`.

### 10. Not implemented / honest

- **No palette row for a server-contributed command.** The rows exist and render
  at `/mcp <label>`; a user cannot fuzzy-find them without already knowing the
  name. The registry question (static rows vs per-session contribution) belongs
  to `cli/commands.py`.
- **No persistent connector session.** Every read is a bounded spawn, which is
  the existing product behaviour and is why the latency figures are
  spawn-dominated.
- **`tools_outside_the_pin` is a read-side projection only.** `_narrow_to_pins`
  in `cli/connectors.py` is what actually moves an unpinned tool out of the
  EXPOSED catalog; the module-level helper is the vocabulary for a surface that
  wants to name the two reasons separately (`not_in_policy`, `outside_pin`).
- **The pin narrows what is EXPOSED, not what is CALLABLE.** The `tools`
  declaration narrows the call path and both are enforced from the same
  declaration, so the surface and the gate cannot drift into different lists.
- **The strict-mode key is read from a task/session config mapping only.** It is
  deliberately not read from a repository file, so a repository cannot opt itself
  into the posture it is subject to.
- **No latency claim** beyond the bounded-read distribution (median ~2.3 s for a
  real spawn-and-list, spawn-dominated) and the 0.235 ms budget receipt. **No
  token claim** beyond the divisor-disclosed ratios above.
