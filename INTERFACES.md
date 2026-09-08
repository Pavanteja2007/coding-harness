# INTERFACES.md — Contract Between the 4 Parallel Workstreams

This file is the source of truth for how the four modules talk to each other.
Build against these contracts, not against each other's internal code. If a
signature must change, update this file first and note it under "Change Log"
so the other three terminals see it.

Repo layout:
```
repo/
  AGENTS.md
  INTERFACES.md
  project-spec.md
  harness/        <- Terminal 1
  execution/      <- Terminal 2
  runtime/        <- Terminal 3
  memory/         <- Terminal 4
  mcp_server/     <- Terminal 4
  cli/            <- Terminal 4
  tests/
  logs/           <- gitignored; structured state files + full traces
```

## Shared data types (all modules import these — put in `shared/types.py`)

```python
from dataclasses import dataclass, field
from typing import Literal, Optional

@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

@dataclass
class VerificationResult:
    target_test_passed: bool
    baseline_passed: bool          # did this test pass BEFORE any edit?
    regression_passed: bool        # did the full suite still pass after?
    flaky: bool                    # same test, different outcomes across reruns
    raw_output: str

@dataclass
class TaskResult:
    task_id: str
    status: Literal["success", "failed", "error", "timeout"]
    attempts: int
    diff: Optional[str]
    verification: Optional[VerificationResult]
    cost_usd: float
    model_calls: list              # list of {step, model, provider, tokens, cost}
    log_path: str                  # path to full structured trace for this task

@dataclass
class Task:
    task_id: str
    repo_path: str
    issue_text: str
    config: dict                   # max_retries, budget_cap, etc.
```

## Boundary 1 — Terminal 1 (harness) calls Terminal 2 (execution)

```python
# execution/sandbox.py — implemented by Terminal 2, called by Terminal 1
def execute_sandboxed(repo_path: str, command: str, timeout_s: int = 120) -> ExecutionResult: ...
    # Keyword-only extras (Terminal 2's real implementation, all default-safe):
    #   allow_network: bool = False      # drop --network none for this call
    #   env: dict | None = None          # extra container env vars
    #   mem_limit / cpu_limit / pids_limit  # resource-limit overrides

# execution/verify.py — implemented by Terminal 2, called by Terminal 1
def verify(repo_path: str, target_test: str, rerun_for_flake_check: int = 1) -> VerificationResult: ...
    # Keyword args the real implementation accepts (matches the stub phase):
    #   test_command: str | None = None     # None = autodetect pytest
    #   verify_timeout_s: int = 300
    #   allow_network: bool = False          # keyword-only
```
**Terminal 2's real implementation is LIVE** (Docker sandbox). Semantics the
harness relies on (confirmed by its test suite + a real-repo DoD run):
- `execute_sandboxed` runs each command in a FRESH container per call, repo
  bind-mounted READ-WRITE at /workspace (agent edits persist to the host —
  the harness diffs pristine/work dirs on the host), resource limits
  (mem/cpu/pids), read-only rootfs, cap-drop ALL, no network unless
  `allow_network=True`. Raises `SandboxUnavailableError` when Docker is
  down — it NEVER silently runs unsandboxed (keep the stub for that).
- `verify()` is a STATELESS evaluator of one repo state: runs the target
  test `max(1, rerun_for_flake_check)` times (flaky = differing outcomes
  across reruns; a timeout counts as a distinct outcome), then the full
  suite for `regression_passed`. `baseline_passed` is ALWAYS False in the
  returned result — only the harness knows the pristine outcome (it calls
  verify() on the pristine copy and fills the field itself).
- Deps: per-repo images built lazily (fingerprint = dep manifests only,
  so code edits never trigger rebuilds) from requirements.txt and
  pyproject [project] dependencies. The repo's own package is NEVER
  installed into the image — tests always exercise the bind-mounted
  source (installed copies would shadow the agent's edits).
- The old stub-phase guidance (build against harness/_stubs) still holds
  for anyone wanting a no-Docker fallback; deps.py resolves real-first.

## Boundary 2 — Terminal 1 (harness) calls Terminal 3 (runtime/model router)

```python
# runtime/model_router.py — implemented by Terminal 3, called by Terminal 1
def call_model(
    messages: list,
    difficulty_hint: Optional[str] = None,   # "easy" | "hard" | None — used by adaptive routing
    provider: Optional[str] = None,          # override; None = let router decide
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str: ...
```
**Until Terminal 3's router exists**, Terminal 1 should build against a stub that just calls
one hardcoded provider directly with the same signature.

## Boundary 3 — Terminal 3 (runtime) calls Terminal 1 (harness)

```python
# harness/core.py — implemented by Terminal 1, called by Terminal 3's scheduler
def run_task(task: Task) -> TaskResult: ...
```
This is THE most important contract in the whole project — the scheduler's entire job is
calling this function many times concurrently with checkpointing around it. Terminal 3 can
build and test its scheduler against a fake `run_task` (sleep + random pass/fail) before
Terminal 1's real one is ready.

## Boundary 4 — Terminal 4 (memory/MCP) reads from Terminal 1's structured state

```python
# harness/context.py — implemented by Terminal 1; format consumed by Terminal 4
# Structured state file written per-task at logs/{task_id}/state.json:
{
  "task_id": str,
  "plan": [str, ...],
  "completed_steps": [str, ...],
  "files_touched": [str, ...],
  "decisions": [str, ...],        # e.g. "chose full-file rewrite over diff for this file"
  "remaining_plan": [str, ...]
}
```
Terminal 4's decision/pattern memory ingests the `decisions` field across tasks over time.
Terminal 1 owns the schema — if it changes, update this file.

## Boundary 5 — Terminal 4 exposes memory to everyone via MCP

```python
# mcp_server/server.py — implemented by Terminal 4
# MCP tools exposed:
#   query_structure(query: str) -> str      # code graph lookups ("what calls X")
#   query_decisions(query: str) -> str      # decision/pattern memory
#   record_decision(text: str) -> None      # called by Terminal 1 after each task
```
Any module (or an external MCP client) can call these once the server is running locally.

## Boundary 6 — Terminal 4's CLI calls into Terminal 3's scheduler and Terminal 1's harness

```python
# cli/main.py — implemented by Terminal 4
# harness fix --repo <path> --issue <text> --model <name>   -> calls harness.core.run_task directly
# harness run-benchmark --subset <name> --concurrency <n>   -> calls runtime.scheduler.run(...)
# harness status --task-id <id>                             -> reads logs/{task_id}/state.json
```

## Change Log
- 2026-09-07 (Terminal 1): harness/_stubs/verify.py (the local stand-in for
  Terminal 2's verify) adds two OPTIONAL keyword args with defaults —
  `test_command: Optional[str] = None` (explicit suite command; None =
  autodetect pytest) and `verify_timeout_s: int = 300` — because the
  harness needs to pass the suite command / timeout per task.config.
  Contract signature is unchanged when they are omitted. Terminal 2: please
  accept the same kwargs (or tell me the canonical way to pass these) when
  the real verify() lands.
  **CLOSED 2026-09-08**: Terminal 2's real execution.verify landed with
  both kwargs, defaults and semantics exactly as requested (node ids
  appended to the caller's test_command). No further action needed.
- 2026-09-08 (Terminal 1): Phase-2 retrieval consumes Terminal 4's
  `memory.code_graph` PROGRAMMATICALLY (new harness->memory dependency,
  beyond the documented Boundary-5 MCP path — per memory/AGENTS.md's
  invitation): `CodeGraph(repo_path, root=<harness-owned dir>)` then
  `load_or_build()`; harness reads the raw `Graph` (nodes/calls/imports
  dataclasses) and never the string `query()` surface. Contract asks:
  keep `Graph.nodes/.calls/.imports` and `NodeInfo.kind/name/qualified/
  file/docstring` stable, or flag here. The harness passes an explicit
  `root` (logs/_code-graph/) — CodeGraph's repo-internal default root is
  never used by the harness (its never-mutate-original-repo guarantee).
  If memory.code_graph is absent/unimportable, retrieval degrades to
  grep-only (never raises).
- 2026-09-07 (Terminal 1): harness.core.run_task has an OPTIONAL second
  param `log_root: Optional[Path] = None` (defaults to ./logs as per the
  repo layout) so tests/runtime can direct logs elsewhere. Single-arg calls
  per Boundary 3 behave exactly as contracted. runtime.worker already
  resolves and calls it successfully.
- 2026-09-07 (Terminal 1): structured state file (Boundary 4) is live at
  logs/{task_id}/state.json in the exact documented schema; the schema was
  NOT changed. Also written per task: trace.jsonl (every prompt/response/
  tool call/result). Note for Terminal 4: state.json lists a file in
  files_touched only after edits pass validation, and completed_steps is
  reset when a retry rolls back work — trust remaining_plan for current
  progress.
- 2026-09-07 (Terminal 1): Boundary 2 usage convention — model modules are
  asked to expose get_last_usage() (the stub does; runtime.model_router
  already does too). harness.model_client reads it after each call for
  budget accounting; returns zeros if absent, never crashes the run.
- 2026-09-07 (Terminal 3): Boundary 2 is LIVE — runtime.model_router.
  call_model implemented (litellm underneath). Signature unchanged.
  One extension: difficulty_hint now also accepts "medium" as a middle
  tier (Boundary 2 documented "easy" | "hard" | None). Callers must treat
  unknown hints as None (router normalizes; never crashes on them).
  Routing semantics: explicit provider/model (call arg or task.config)
  always beats the hint; hint beats defaults. When adaptive_routing is
  on and no hint is given, the router predicts difficulty from message
  content (heuristic estimator default). Config keys consumed:
  adaptive_routing, model_tiers, difficulty_estimator, difficulty_llm,
  provider/model/api_key (all via task.config; documented in
  runtime/config.py).
- 2026-09-07 (Terminal 3): Boundary 3 caller side is LIVE —
  runtime.scheduler.run(tasks, concurrency, logs_root, run_id) spawns
  one `python -m runtime.worker` process per task, each calling the
  Boundary-3 run_task (real harness.core first, runtime/fake_harness
  fallback). Worker calls run_task(task, log_root) when the callable
  accepts log_root (T1's optional param is honored), else run_task(task).
  Terminal 4: this is the Boundary 6 run-benchmark entrypoint.
- 2026-09-07 (Terminal 3): runtime file layout — runtime bookkeeping for
  a task lives in logs/{task_id}.runtime/ (checkpoint.json, heartbeat.
  json, events.jsonl, model_ledger.jsonl, approval/), deliberately a
  SIBLING of the harness's logs/{task_id}/ because core._fresh_paths
  ARCHIVES logs/{task_id}/ wholesale on every relaunch. Terminal 1:
  that same archiving means state.json does not survive a relaunch, so
  until run_task grows resume support (skip completed steps when
  task.config["resume"] is True — see runtime/AGENTS.md), a relaunched
  task restarts from scratch. The runtime already handles the resume
  side; T1 owns the harness side.
- 2026-09-08 (Terminal 1): RESUME CONTRACT LANDED — the Change Log entry
  above is CLOSED. harness.core.run_task now implements the resume side:
  when task.config["resume"] is truthy and logs/{task_id}/ holds a
  state.json with completed steps + a plan.json (new, harness-internal —
  NOT part of the Boundary 4 schema), the relaunch CONTINUES instead of
  archiving: persisted plan reused (no re-planning), completed steps
  skipped, surviving pristine/work dirs kept (partial edits built upon),
  in-flight attempt + spent budget continued, trace.jsonl appended to.
  Verified by a real hard-kill (os._exit in a child process) mid-run +
  relaunch test mirroring Terminal 3's scheduler integration. Notes:
  (a) resume consumes no harness max_retries slot — a crash is an
  interruption, not a verification failure (the runtime's crash_retries
  remains the crash budget); (b) baseline verify is skipped on resume
  (a passing baseline would have ended the pre-crash run before any
  step completed); (c) resume auto-degrades to a fresh start when
  state.json/plan.json are unreadable or pristine/work are missing
  (trace events: "resume_aborted");   (d) with resume falsy/absent the
  old archive-as-{task_id}.old-* behavior is unchanged. Terminal 3:
  your worker can now resolve resume against the REAL harness; no
  runtime-side changes needed. Terminal 4: unaffected — state.json
  schema unchanged.
- 2026-09-08 (Terminal 1): harness-internal file
  `logs/{task_id}/plan.json` (parsed planner steps + in-flight attempt +
  spent cost_usd; written by the harness, consumed only by resume). NOT
  part of the Boundary 4 schema — do not parse it from outside the
  harness. Also new: shared structural index root `logs/_code-graph/`
  (see the memory.code_graph entry above) — repo-keyed subdirs, shared
  across tasks; harmless to prune (rebuilds lazily).
- 2026-09-07 (Terminal 3): config keys the runtime reads/adds via
  Task.config (beyond harness's own): crash_retries (scheduler crash
  budget — NOT harness max_retries), resume (bool), resume_dir,
  approval ("require"), approval_timeout_s, hang_heartbeat_stale_s,
  log_root, plus the routing keys above. All documented in
  runtime/config.py; unknown-key passthrough applies.
- 2026-09-07 (Terminal 3): dependency note — litellm is PINNED to
  1.74.9 on this machine: 1.100.0 imports `NotRequired` from `typing`
  and breaks on Python 3.10. Keep lazy litellm imports (T1's stub
  pattern) when upgrading.
- 2026-09-08 (Terminal 3): Boundary 2 extension — `model_tiers` entries
  may now carry their own `api_key` and `api_base` (per-tier endpoints),
  and `api_base` is read at context level too. call_model's signature
  is unchanged. New retry behavior inside call_model: provider 429s
  back off exponentially (`rate_limit_retries` default 4,
  `rate_limit_backoff_s` default 15); transient upstream errors (5xx,
  connection errors, empty-message gateway flakes) get 2 short retries.
  All documented in runtime/config.py.
- 2026-09-08 (Terminal 3): Scheduler now PINS `resume_dir` and
  `log_root` into each task's config at spawn (task-config overrides
  still win) — without this, a scheduler given a non-default logs_root
  and its workers used DIFFERENT trees for checkpoints/ledgers (real
  bug found running the first ablation). Also new public scheduler
  API: `Scheduler.live_attempts() -> {task_id: _Attempt}` for external
  supervisors (used by runtime/stress.py's fault injector).
- 2026-09-08 (Terminal 3): First REAL ablation data (Phase 5): the 5
  fixture bugs through the full real stack, adaptive routing v2 ON vs
  always-expensive OFF → 100% success BOTH arms, ON at 45% of OFF's
  cost (proxy prices; see runtime/AGENTS.md "Round 2" + the per-arm
  summaries under logs/ablations/v2-heuristic-*). Predictor v2 redesign
  documented in runtime/difficulty.py: intrinsic signal from the issue
  text only (cuts at `## Retrieved context`), struggle signal from
  conversation tail. Terminal 1: please keep the planner prompt's
  `## Issue` / `## Retrieved context` section headers stable — the
  predictor keys on them (mirrored in difficulty._SCAFFOLD_MARKERS).
- 2026-09-07 (Terminal 2): **Boundary 1 integration CONFIRMED with Terminal 1's
  real harness** (Round 2, Tasks A/B). (a) kwarg contract: real
  execution.verify.verify accepts `test_command` and `verify_timeout_s`
  (positional-or-keyword, same defaults as the stub) — T1's three call
  sites (core.py baseline/final/step verify) confirmed compatible; no
  changes needed on either side. (b) Swap: deps.py's real-first resolution
  already routes execute_sandboxed + verify to the real Docker
  implementation; T1's FULL test suite (56 tests incl. 5 e2e bug-fix runs)
  re-executed under real sandbox-driven load: all pass; live docker
  sampling confirmed fresh hexec-* containers per e2e test; zero leaked
  containers; per-fixture dep images pre-warmed via ensure_image (batch
  warm-up pattern for Phase 3 benchmarks). No contract changes in this
  round. Note: the T1 kwarg request from the earlier change-log entry is
  RESOLVED — the kwargs are the canonical way to pass suite command and
  verify timeout.
- 2026-09-07 (Terminal 2): Boundary 1 is LIVE. execute_sandboxed keeps
  the exact contract signature (timeout_s positional-or-keyword) and
  adds keyword-only optional extras (allow_network, env, mem/cpu/pids
  limits) — all default-safe for contract callers. Docker semantics:
  fresh --rm container per call, repo bind-mounted RW at /workspace
  (host persistence REQUIRED by T1's pristine/work diffing),
  --network none by default, resource limits, read-only rootfs.
  Raises SandboxUnavailableError (never silent unsandboxed fallback).
- 2026-09-07 (Terminal 2): verify() accepts the stub-phase kwargs
  formally: test_command, verify_timeout_s, and keyword-only
  allow_network (T1's core.py already passes the first two). Division
  of labor fixed: verify() is stateless per repo state — the target
  test runs max(1, rerun_for_flake_check) times (flaky = mixed
  outcomes; timeout counts as a distinct outcome), then the full suite
  for regression_passed; baseline_passed is ALWAYS False from verify()
  and the HARNESS fills it from its pristine-copy call (T1's core.py
  already does exactly this via _with_baseline).
- 2026-09-07 (Terminal 2): module-level note — `execution/__init__.py`
  deliberately does NOT re-export `verify` (it would shadow the
  submodule). Import submodules directly:
  `from execution.sandbox import execute_sandboxed`,
  `from execution.verify import verify`.
- 2026-09-07 (Terminal 2): new module surfaces beyond the Boundary-1
  contract (this module's own scope, callable by T1/T4/CLI):
  execution.git_output.produce_git_output(work_dir, issue_text,
  changed_files, diff, verification_summary, rationale, branch_name,
  pristine_dir) -> {branch, commit_sha, commit_message, pr_description};
  execution.rationale.build_rationale(log_dir, issue_text=None) -> str
  (reads logs/{task_id}/trace.jsonl + state.json, the Boundary-4/trace
  formats). See execution/AGENTS.md.
- 2026-09-07 (Terminal 4): Boundary 5 MCP tools are LIVE (mcp_server/
  server.py, MCP Python SDK 2.x — MCPServer; FastMCP v1 import kept as
  fallback). Compatible extensions to the documented signatures:
  query_structure(query, repo="") — optional repo path; omitted = last
  indexed repo; first call against a repo builds the tree-sitter graph
  under HARNESS_HOME/code-graph/. query_decisions(query="") — empty
  query returns recent entries; every call first lazily ingests new
  logs/*/state.json decisions (Boundary 4). record_decision(text,
  category="general") -> str — returns a confirmation string (was
  documented `-> None`); category is a free-form grouping label.
  Two extra tools beyond Boundary 5 (spec item 31): task_status(task_id)
  (state.json summary) and list_repos(). Run standalone:
  `python -m mcp_server` (stdio).
- 2026-09-07 (Terminal 4): Boundary 6 CLI is LIVE (cli/main.py, entry
  `harness` / `python -m cli`). fix calls harness.core.run_task directly
  (passes log_root only when --log-root given); run-benchmark calls
  runtime.scheduler.run via signature probing (real scheduler's dict
  return normalized to task order); status reads
  logs/{task_id}/state.json + enriches from trace.jsonl. Note for
  Terminal 3: the scheduler runs workers in subprocesses, so per-task
  model stubs can't be injected in-process — run-benchmark's 'smoke'
  subset uses use_fake_harness (per runtime/AGENTS.md) for offline runs.
- 2026-09-07 (Terminal 4): decision DB location convention — SQLite at
  HARNESS_HOME/memory/decisions.db (HARNESS_HOME defaults to ./.harness
  under cwd; overridable via HARNESS_HOME / HARNESS_DECISIONS_DB /
  HARNESS_LOGS_DIR env vars — see memory/paths.py). .harness/ is
  gitignored. Terminal 1: record_decision calls (Boundary 5, "after
  each task") can go through the MCP server OR call
  memory.decision_store.DecisionStore directly — but state.json
  decisions are already auto-ingested on every query_decisions call,
  so direct calls are only needed for facts NOT in state files.
- 2026-09-08 (Terminal 4): Boundary 4 ingestion is now RECURSIVE —
  DecisionStore.poll() scans logs_dir with rglob("state.json") at ANY
  depth. Reason: real runs nest state files (T3's ablation driver stages
  them at logs/ablations/<run>/tasklogs/<task_id>/state.json); the old
  1-level glob silently missed them. Any driver that writes state.json
  under the shared logs root is auto-ingested now.
- 2026-09-08 (Terminal 4): Boundary 6 CLI extensions — `harness fix` now
  accepts `--api-base <url>` (custom model endpoint; maps to
  Task.config.api_base) and `--adaptive-routing`; in-process fix runs
  install the runtime router context around run_task exactly like
  runtime.worker does (set_call_context with provider/model/api_key/
  api_base/adaptive_routing/model_tiers), cleared after the run. Without
  this, in-process runs couldn't reach BYO-router endpoints. Verified
  with a real end-to-end fix (cloud model via tokenrouter: success,
  $0.0093, correct diff).
- 2026-09-08 (Terminal 4): note for anyone running real-model benchmarks
  through the scheduler: workers must raise hang_heartbeat_stale_s
  (default 30s kills the real harness during long model calls before it
  touches state.json — T3's documented granularity limit). Set
  hang_heartbeat_stale_s: 300 in task configs for real-model runs.
- 2026-09-08 (Terminal 4): **REPAIRED harness/context.py mid-integration —
  full detail for Terminal 1's review.**
  - **What was broken**: an interrupted edit (T1's in-flight resume work,
    around the plan.json/`save_plan_steps` feature) left context.py with a
    duplicated dangling `def _write(self) -> None:` stub (empty body after
    the docstring'd `save_plan_steps`), making the module un-parseable —
    every `import harness.*` in the repo raised IndentationError. Found
    while running the real CLI e2e (Round 2), not by inspection.
  - **Exact change made**: DELETED the dangling duplicate `def _write`
    stub ONLY — nothing else. No lines added, no signature changes, no
    schema changes. After the deletion: `save_plan_steps(...)` (ends at
    its `write_text` block) followed by the real `def _write(self)` at
    what is now context.py:196, containing the full Boundary-4 atomic
    write (tmp + replace). `git diff` at repair time showed exactly the
    one deletion.
  - **Why this is safe/minimal**: the surviving `_write` is the ONLY
    method writing state.json, and its body (key order, tmp+replace
    atomicity, `with_suffix('.json.tmp')`) is unchanged from before the
    interrupted edit. `TaskState.__init__`'s resume-hydration branch,
    `read_state`, `read_plan_bookkeeping`, `save_plan_steps`, and all
    public mutation methods are intact and importable.
  - **Verification**: `python -c "import harness.context"` clean after
    the repair; T1's own resume test suite (`tests/test_config_trace_state
    .py` + `tests/test_e2e_run_task.py`, incl. the hard-kill/relaunch
    resume test) pass on the repaired file; the CLI's real e2e fix run
    (real cloud model, real Docker verify) completed against it.
  - **T1 action needed**: none unless your intended final state differs —
    please just confirm the deletion matches your intended final shape of
    `_write`/`save_plan_steps` (i.e., that the duplicated stub was an
    artifact of the interrupted edit, not a method you meant to keep).
- 2026-09-08 (Terminal 4): new module surface — `dashboard/` (spec
  stretch item 40, explicitly read-only). `dashboard.collect.scan_logs
  (logs_dir) -> list[dict]` + `group_by_run` + `aggregate` summarize the
  EXISTING Boundary-4 formats (state.json/trace.jsonl) plus Terminal 3's
  documented `.runtime/` bookkeeping (model_ledger.jsonl,
  checkpoint.json); nothing new is written anywhere. `harness dashboard`
  (new CLI subcommand) / `python -m dashboard` serves a GET-only web
  view. No contract changes — this only READS the documented formats.
  Terminal 3: if your model_ledger/checkpoint field names change, this
  is the second consumer (after your own ablation tooling).
- 2026-09-08 (Terminal 4): real-integration status — CLI fix +
  run-benchmark are verified end-to-end against the REAL harness,
  REAL Docker sandbox, REAL scheduler, and a REAL cloud model (no
  stubs, no injected fakes anywhere). MCP server verified against a real
  external stdio client on production data. Details in each module's
  AGENTS.md.
