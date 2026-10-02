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

## Boundary 0 — versioned agent-kernel contracts

`shared/agent_contracts.py` is the canonical, dependency-light contract for
agent sessions and runs. `harness.agent_kernel` re-exports these types for
compatibility; it does not define a second schema. Every serialized contract
carries `schema_version = 1` and rejects unsupported versions.

```python
from shared.agent_contracts import (
    SCHEMA_VERSION,
    Checkpoint,
    CompletionStatus,
    PermissionDecision,
    RunEvent,
    RunResult,
    RunSpec,
    SessionState,
    ToolCall,
)
```

`CompletionStatus` is exactly:

```text
completed_verified | completed_unverified | needs_input | blocked |
failed | cancelled | timeout
```

- `RunSpec` is JSON-only session/run/request identity plus strategy, workspace,
  verification, resume, and metadata policy. Runtime callbacks/services are
  constructor-injected and are not serialized into it.
- `SessionState` is bounded conversation continuity (summary, recent turns,
  active run, changed files, plan/todo, unresolved questions).
- `ToolCall` and `PermissionDecision` are typed, versioned tool/policy records.
- `Checkpoint` records the last durable event sequence, resume references, and
  additive `repository_identity`, `request_identity`, `revision_identity`, and
  `resume_namespace` fields. Resume validation requires all identity fields;
  only the explicit `continue` compatibility request may reuse its stored
  request hash.
- `RunResult.status` is a `CompletionStatus`; `completed_verified` is emitted
  only from clean verifier evidence. A model `finish`/`done` request without a
  declared verifier is `completed_unverified`, never verified.
- `RunEvent` rows are the single append-only authority at
  `logs/{task_id}/trace.jsonl`. Each row includes `sequence`, `schema_version`,
  `session_id`, `run_id`, `turn_id`, `event`, and `payload`, plus legacy
  `kind`/`data` aliases. `harness.agent_kernel.replay_run(path)` validates
  identity/version/contiguous sequence and returns a deterministic semantic
  projection without executing tools or calling a model.
- `AgentKernel` strategy names are `daily`, `verified_fix`, `planning`,
  `question`, `research`, and the compatibility adapter `legacy_agent`.
  Unknown names fail before a run directory is created.
- Public compatibility remains exact: `harness.core.run_task(Task) ->
  TaskResult`, `harness.agent_loop.run_agent(...) -> AgentResult`, the six-key
  `state.json` prefix, and the existing trace aliases. The historical fix and
  general-agent implementations are private strategies/adapters, not parallel
  public architectures.
- Kernel packages never import `cli`; UI/session/plugin/MCP callbacks cross the
  boundary as injected services or resolver callables.

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
- **R2-02 addendum — the flake verdict is THREE-valued, and this signature is
  on its way out from under the 0/1-means-one-run reading.** Because
  `max(1, rerun_for_flake_check)` makes `run_count == 1` for every value in
  `{0, 1}`, `flaky = len(set(outcomes)) > 1` is unsatisfiable at those
  values, so `flaky=False` there means "NOT CHECKED", not "stable". The
  repetition/verdict layer now lives in `execution/flake_gate.py`:
  `flake_verdict(repetitions, outcomes)` returns `flake_check` in
  `{"flaky_detected", "not_flaky", "not_run"}`, where `not_run` is the
  `run_count < 2` case, and derives the verdict from the number of outcomes
  OBSERVED so a caller bug degrades to `not_run` rather than to a false
  "not_flaky". `repetitions_for_stage(stage, config)` resolves how many runs a
  stage performs: `baseline` defaults to 1 (a cheap baseline answers its own
  question with one run), `post_fix` defaults to 2 (the minimum that can
  fire). Callers should pass the resolved count here and read `flake_check`
  — never `flaky` alone — when they intend to claim stability was shown.
  The `verify()` signature above is unchanged by this round; see the
  2026-09-26 R2-02 Change Log entry for the call sites and the measured cost.
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
  "remaining_plan": [str, ...],
  # ADDITIVE keys (written AFTER the six above, only when set —
  # consumers MUST treat extra keys as ignorable):
  "repo_path": str,               # the task's repo (memory scopes decisions by it)
  "change_groups": {              # Improvement Round 2: declared ATOMIC multi-file
    "<group-name>": ["file1.py", "file2.py", ...]  # change units from the plan —
  }                                # validated together, rolled back together
}
```
Terminal 4's decision/pattern memory ingests the `decisions` field across tasks over time.
Terminal 1 owns the schema — if it changes, update this file.
The six-key prefix order is the stable contract; `repo_path` and
`change_groups` are additive and absent when unset (single-file fixes
write neither, or only repo_path).

## Boundary 5 — Terminal 4 exposes memory to everyone via MCP

```python
# mcp_server/server.py — implemented by Terminal 4
# MCP tools exposed (all return str):
#   query_structure(query: str, repo: str = "") -> str
#   query_decisions(query: str = "", repo_path: str = "") -> str
#   record_decision(text: str, category: str = "general",
#                   repo_path: str = "") -> str
#   task_status(task_id: str) -> str
#   list_repos() -> str
```
Any module (or an external MCP client) can call these once the server is running locally.

**Consumption note (Round 2, memory-informed planning):** Terminal 1's
PLANNER now actively queries this memory surface before planning — see the
Change Log entry dated 2026-09-10. It calls
`memory.decision_store.open_default_store()` programmatically (NOT the MCP
wire — same process tree, no server round-trip needed) and
`DecisionStore.search(query, limit, repo_path=...)` with the task's repo
for scoping. Terminal 4: keep `search`'s repo_path kwarg and
`open_default_store` stable, or flag here.

The additive `repo_path` arguments on `query_decisions` and
`record_decision` are optional for backward compatibility. When omitted,
the historical global decision behavior remains; when present, reads and
writes are scoped to the canonical repository. `query_structure`'s
optional `repo` argument reuses the last indexed repository only when it
is omitted.

## Boundary 6 — Terminal 4's CLI calls into Terminal 3's scheduler and Terminal 1's harness

```python
# cli/main.py — implemented by Terminal 4
# neo fix --repo <path> --issue <text> --model <name>   -> calls harness.core.run_task directly
# neo run-benchmark --subset <name> --concurrency <n>   -> calls runtime.scheduler.run(...)
# neo status --task-id <id>                             -> reads logs/{task_id}/state.json
```

## Change Log
- 2026-10-01 (P0-W2-T1 - Wave 2 pins for the harness module): **FOUR NEW
  `harness/`-local test modules (`harness/test_egress_call_sites.py`,
  `harness/test_mint_site_pins.py`, `harness/test_gap_audit.py`,
  `harness/test_structural_claims.py`); NO production file edited; no Boundary
  0-5 signature, event kind, journal field, serialized contract field, or
  completion status changed; no `harness/config.py` `DEFAULTS` key added; no
  prompt changed.** `cli/**`, `execution/**`, `runtime/**`, `shared/**`,
  `memory/**`, `evals/**`, `tests/**`, `scripts/**` and `.github/**` were read
  and not written. This entry is a record, not a contract change. (1) **The
  redaction boundary is pinned STRUCTURALLY.** `harness/test_egress_call_sites.py`
  parses every `.py` under `harness/` with `ast` and classifies each carrier-bearing
  egress site into four verdicts — `redacted` (routes through a redaction helper
  inline), `redacted_by_receiver` (the SINK redacts the whole payload, proven live
  per receiver), `allowlisted` (a written reason plus an owner), `unredacted`
  (the failure). Measured today: **19 sites, 0 unredacted, 7 allowlisted, 13
  redacted by the sink.** A bare `append` is NOT a sink: the discriminator is
  `JOURNAL_RECEIVERS` plus a string-literal event-kind first argument, because
  `harness/` has ~200 `x.append(...)` calls and `messages.append(...)` is the
  model's own context window, which must stay verbatim. (2) **One real
  un-redacted egress site, RECORDED not fixed:** `harness/tools.py::_
  precommit_refusal` concatenates the in-edit gate receipt's `context` — real
  source bytes from `lint._context_block` — into a `ToolResult` without
  redacting, while `lint.render_check` redacts the very same field of the very
  same receipt. Reachable through `TypedToolRuntime.execute`, whose only callers
  today are tests. Owner T1, one line. `RECORDED_GAPS` holds it with a
  behavioural proof that asserts the secret still SURVIVES. (3) **The mint count
  is pinned at FOUR, and the doctrine's "one" is pinned as a gap.**
  `MINT_SITES_EXPECTED = 4` plus the four named mint files; a second pin reads
  `phases/DOCTRINE.md` and fails if the constant becomes 1 while the doctrine
  still claims "exactly one place", so a legitimate collapse cannot leave the
  documentation stale. Both recorded condition divergences — `legacy.py`
  defaulting a MISSING `regression_passed` to `True`, and `verified.py` +
  `legacy.py` reading an error-bearing evidence block as clean — are asserted as
  EXACT facts with inverted pins, so a tightening must update the record. (4)
  **Every TODO/FIXME/HACK/placeholder in `harness/` is classified:** ten hits in
  comments and docstrings, all ten DECISIONS with written reasons, zero
  marker-word gaps — including the case a grep cannot get right, since the typed
  catalog has a tool literally named `todo`. A **second** gap was found by
  RUNNING `pytest harness/`: `harness/test_config.py` is a production module
  named `test_*.py`, so pytest collects it and then errors on its own public
  `test_config_guard(pristine_dir, work_dir)` API. Recorded in `GAPS` with an
  inverted pin that runs pytest and asserts the error is still there. (5) **The
  three structural claims are pinned by name:** journal-redactor linearity with a
  4x RATIO assertion (an absolute budget alone does not catch a quadratic
  return at the sizes measured); the four `step()` purity pins verified present
  in `tests/test_agent_loop_matrix.py` (T5's lane, not edited here) and
  additionally verified COLLECTIBLE; and `LoopEnvironment.approve()` asserted to
  answer `False` when called on the base class AND on a subclass that overrides
  only `ask`/`invoke`. **No CI wiring was added** — `.github/**` is outside this
  terminal's ownership; see `harness/AGENTS.md` for the exact selection command
  T5 needs.

- 2026-09-29 (VEX-PF-02 - `/connect`, the opencode-style authentication flow):
  **NEW `cli/auth.py` and NEW `tests/test_auth_flow.py`; `cli/onboard.py`
  edited; `cli/commands.py` edited ADDITIVELY; `cli/main.py` given TWO
  additive blocks (disclosed); `cli/connectors.py` NOT edited although it is
  in the file list. `cli/tui.py` was NOT opened.** **No Boundary 0-5
  signature, event kind, journal field, serialized contract field, completion
  status, verifier mint, or `harness/config.py` `DEFAULTS` key changed, and
  no prompt changed**, so this entry is a record rather than a contract
  change. (1) **SAVE FIRST, TEST SECOND.** `cli.auth.connect()` persists the
  credential and returns a receipt; verification happens afterwards on a
  daemon thread against a CHILD PROCESS (`python -m cli.auth --probe`), so a
  failed check can never discard what the user typed and a provider banner
  is structurally unable to reach a terminal. A child process rather than a
  thread specifically because `redirect_stdout` swaps `sys.stdout`
  process-wide and would have swallowed the app's own output. (2) **One
  store:** `<neo_home>/auth.json`, 0600, keyed by provider id with trailing
  slashes normalised, each entry discriminated by `method` in
  `api_key | api_base | env | none`. `save_credential` assigns exactly one key
  under `cli.neoconfig.settings_lock` (reused, not reinvented) — a first run of
  the suite's four-concurrent-writers test LOST a provider, which is what
  found the missing lock. (3) **The provider list is data:** a table plus
  `<neo_home>/providers.json` merged over it, so adding a provider is a data
  edit; sorted by priority then name, the MENU caps at 8 of 9 shipped rows and
  `other` always survives the cap. (4) **The first run never blocks:**
  `onboard.maybe_onboard_repl` no longer runs a wizard; it prints one 74-char
  line. (5) **Plain sentences only:** `classify_failure` reduces an exception
  object, an exception string, a provider banner or a closed `FAILURE_KINDS`
  word to one sentence, and DROPS the provider's text rather than sanitising
  it. (6) **A literal key is written only into `auth.json`.** The settings
  chain receives only `model`/`provider`/`base_url`; a key reaches a run
  through `auth.apply_active_credential()`, and a config file references a
   secret with `{env:VAR}`. (7) `/connect` is registered in
  `cli/commands.py` (`headless_policy="flag-only"` -> `neo connect`,
  `in_flight_policy="allow"`, alias `/auth`); it is deliberately NOT added to
  `REQUIRED_COMMANDS`, whose exact set is pinned by another terminal's test.
  The REPL and TUI dispatch lines are MOUNT POINTS recorded in
  `cli/AGENTS.md` under "Handoff to 01" — `cli/interactive.py` and
  `cli/tui.py` were being edited concurrently and were not opened. Verified:
  the two required commands -> 135 passed / 1 skipped and 109 passed /
  1 skipped; a combined required lane -> 425 passed / 2 skipped; two
  nonstandard-order shuffles -> 244 and 179 passed; eight regression lanes
  from 75 to 305 passed each; scoped `ruff check` clean, `ruff format` on the
  two new files, `compileall` clean, scoped `git diff --check` exit 0.
  **No Docker lane, no live-provider lane with a real credential, and no
  `evals.run` were run**; the one real probe measured 6938 ms against
  OpenRouter's public endpoint with a fake key and is reported as a
  measurement, not a pass.
- 2026-09-29 (AGT-11 - the agent loop as one pure function, plus the agent
  regression matrix): **NEW `harness/agent_loop_step.py`; `harness/agent_loop.py`
  gains `HarnessToolbox` / `run_agent_stepped` / `STEPPED_STRATEGY`
  (`"agent_step"`) and re-exports the reply parser. No Boundary 0-5 signature,
  event kind, serialized contract field, completion status, or verifier mint
  changed; the append-only `trace.jsonl` is still the durability boundary and
  was NOT rebuilt. `harness/core.py`, `harness/agent_kernel/**`,
  `harness/config.py`, `execution/**` and every verifier were NOT edited, and
  the legacy `_run_agent_legacy` loop is byte-identical.**
  (1) **One pure decision function.**
  `step(history, tools, config) -> events` is a total function of its three
  arguments: no I/O, no clock, no globals, no randomness. The model, every
  tool, the elapsed-seconds reading, the spend meter, the cancel flag, the
  approver and the steering inbox all arrive through ONE injected boundary
  (`LoopEnvironment`), whose defaults are chosen in the safe direction -
  `approve()` returns **False**, so a run with no approver refuses rather than
  proceeding. `step` runs the loop to its terminal event and returns the whole
  list; `task_end` appears exactly once and is always last; every kind is in
  the closed set `EVENT_KINDS`. Purity is pinned four ways, because a comment
  is not evidence: an import pin, a module-global pin, a determinism pin, and
  a poisoned-`time`/poisoned-`socket` pin.
  (2) **The verifier gate is structural, not documented.** The terminal
  vocabulary IS `shared.agent_contracts.RUN_STATUSES` - it contains no bare
  "the run worked" word - and `status_is_success()` is true for exactly
  `completed_verified`, reachable from exactly one place: a declared verifier
  returning clean evidence. A run with no declared tests is
  `completed_unverified` plus a `verify_skipped` event that names itself, and
  `agent_loop_step.py` contains **no string literal equal to `success`
  outside its own docstrings** (a source-level pin).
  (3) **A second mint-condition divergence, found and recorded, not hidden.**
  `harness/agent_kernel/legacy.py` reads only the three booleans, so it treats
  an evidence block that also carries an `error` as clean; the pure core
  requires the error to be absent. The pure core is the strict side, and the
  divergence is asserted EXACTLY by
  `tests/test_agent_loop_matrix.py::test_the_three_mint_conditions_agree_across_the_tree`
  (which also records the second, older divergence: legacy defaults a MISSING
  `regression_passed` to True, the pure core treats an absent term as False).
  (4) **A bug this round's own suite found in its first draft of `step`.** The
  reflection stop-condition was `not reflection.allowed`, which is the wrong
  flag: `allowed=False` also means "this is a refusal, do not invite a retry",
  so the loop ended `failed` on the first policy refusal, an approver denial
  or an environment fault. The correct predicate is `not reflection.exhausted`
  - a cap is the only thing that stops the run, exactly as AGT-02's `_reflect`
  already worked. A refusal is now reported and the loop CONTINUES, and it
  still spends no budget.
  (5) **The reply grammar moved, and was proven to move unchanged.**
  `parse_tool_call` and its tables now live in `agent_loop_step.py` and are
  re-exported from `agent_loop`, so `from harness.agent_loop import
  parse_tool_call` (used by `tests/test_agent_loop.py`) keeps working. A
  differential against the pre-move implementation - 35 hand-written replies x
  two verb sets, plus every string of length <= 3 over a grammar-shaped
  alphabet - reported **0 divergences**.
  (6) **The matrix, run twice per scenario.** NEW
  `tests/test_agent_loop_matrix.py` -> **95 passed, 2 skipped** (host-only: no
  Docker, no provider, no network; the two skips are the `cancel` row's
  real-adapter runs, which need a real in-flight interrupt and are covered by
  `tests/test_agt_10_batch_boundary.py` and `tests/test_steering.py`). The
  fourteen required scenarios - question, research, build, fix, multi-file,
  tool failure, policy refusal, context exhaustion, resume, cancel, doom-loop,
  unparseable tool call, approver denial, network unavailable - are a table
  that is asserted to BE the required table, and each runs twice: against the
  pure function with a scripted boundary, and through the real
  `run_agent_stepped` on a real repository with a real journal, the real model
  boundary, the real executor and the real verifier seam. The two statuses must
  agree, which is what proves the adapter contains no decision. Every case also
  asserts the refusals-vs-budgets rule (a non-retryable failure is FREE) with
  the charged arm asserted too, so it cannot pass by never charging anything.
  (7) **The UXP corpus, executed.** The seven phrasings that broke in real use
  (transcribed with their original incident notes from the classifier tables)
  are cases: classified per-sentence, run through the real adapter, and
  asserted never to mutate a repository on their own. Three further phrasings
  the classifier does NOT route yet are recorded as
  `KNOWN_CLASSIFICATION_GAPS` with an INVERTED pin that fails the day someone
  fixes one, telling them to promote it into the corpus - a recorded gap is one
  somebody can close; an unrecorded one just gets rediscovered.
  (8) **Adapter bugs this round found and fixed, now pinned.** `_run_verify`
  emitted its own `verify` row on top of the core's, putting two journal rows
  for one verifier run in the authoritative journal; the adapter now passes it
  a no-op emit. And the executor's changed-file delta was never handed to the
  core, so the terminal receipt's `files_touched` was empty on the real path
  while the result carried the list; the delta is now attached to the
  `ToolOutcome` detail and the journal receipt and the result are asserted
  equal.
  (9) **What is deliberately NOT migrated.** `_run_agent_legacy` is
  byte-identical and `agent_step` is an explicit pin, never a default:
  switching every run onto the new core is a separate, measured decision. So
  `harness/agent_loop.py` is "adapters only" for the NEW code and still
  contains the old loop; the remaining gap is filed in `harness/AGENTS.md`
  AGT-11 section 9. `harness/core.py`'s fix loop is untouched. Verified: NEW
  suite 95 passed/2 skipped; `test_agent_loop` + `test_agt_02_reflection` +
  `test_agt_10_batch_boundary` + `test_config_trace_state` -> **121 passed**;
  `test_agent_kernel` + `test_tool_protocol` + `test_verification_gate_wiring`
  + `test_agent_loop_matrix` -> part of a 103-passed run whose one failure is
  `test_verification_gate_wiring.py::test_a_run_with_no_intelligence_config_is_byte_identical`,
  a **Docker-daemon-down** `SandboxUnavailableError` (the daemon is not
  reachable on this host - a blocked lane, not a pass); `test_modes` +
  `test_cli_runview` + `test_cli_tracelog` + `test_ceiling_r2_04_daily_default`
  -> **258 passed, 2 skipped**; `test_capability_surface` +
  `test_cli_command_system` + `test_ceiling_r2_03_config_guard` +
  `test_stubs_and_deps` -> **216 passed, 1 skipped**; `test_steering` +
  `test_recovery_steering` -> 101 passed / 7 skipped with one failure,
  `test_two_502s_recover_through_the_real_fix_loop`, the same Docker gate on
  the `harness/core.py` path; `test_evals_run` + `test_evals_tasks` +
  `test_daily_driver_evals` -> 67 passed with the TWO pre-existing
  `test_daily_driver_evals` failures recorded four rounds running (AGT-03/04/
  05/07 all name them): they turn on `status == success` against the honest
  `completed_verified` on the legacy `harness.core.run_task` path, which loads
  **no** `harness.agent_loop*` module at all (verified by import probe) and
  which no `agent_step` config can reach. `python -m evals.run --check` ->
  **14/14 CLEAN**, exit 0. `ruff check` clean on all three owned files; the two
  NEW files are `ruff format` clean and `harness/agent_loop.py`'s own hunks
  were hand-formatted (the file holds pre-existing whole-file format debt from
  parallel terminals, so no formatter was run over it); `compileall` clean.
  **No Docker lane and no live-provider lane were run and neither is claimed.**
- 2026-09-29 (VEX-TERM-UX-07 round 2 - TUI/REPL/headless parity):
  **`cli/commands.py` (one new `/watch` `CommandSpec` row, the
  `command_record` / `command_failure` / `command_verdict` /
  `is_custom_command_line` / `unknown_command_line` public functions, the
  `SURFACES` / `COMMAND_STATUSES` / `NO_RUN_VERDICT` vocabulary, ONE
  additive `HEADLESS_FLAG_EQUIVALENTS` row for `/effort`, a
  four-direction import-time registry validation),
  `cli/command_exec.py`, `cli/interactive.py`, `cli/tui.py`; NEW
  `tests/test_cli_terminal_parity.py` cases; evidence drivers under
  `logs/terminal-ux/`. **No Boundary 0-5 signature, no event kind, no
  journal field, no serialized contract field, no completion status, and no
  verifier mint changed.** `harness/`, `runtime/`, `memory/`, `execution/`,
  `shared/`, `mcp_server/`, `acp/`, `agent_sdk/`, `evals/`, and
  `cli/session.py` were NOT edited.**
  (1) **One record, built in one place.** `commands.command_record(...)` is
  the single entry point all three surfaces use to finish a command; it
  resolves `presentation` and `recovery` FROM the `CommandSpec` and routes
  an escaping handler through `command_failure`. A source pin asserts
  `command_exec.py`, `interactive.py`, and `tui.py` each call it and none
  calls `command_outcome` directly, because a behavioural test passes for
  every status anybody happened to exercise.
  (2) **One exception meaning.** `command_failure(exc)` returns the single
  `(status, exit_code)` an escaping handler means, reusing
  `cli.exit_codes.classify_exit_code` and special-casing
  `KeyboardInterrupt` to `("cancelled", 130)`. Measured before the fix: a
  `Ctrl+C` was `error`/1 in BOTH shells and `cancelled`/130 headless, and
  a `SandboxUnavailableError` was `error`/1 in both shells and `error`/3
  headless — so a script and a terminal disagreed about which door the user
  was standing in.
  (3) **The verdict is derived from the RUN, and it is on every surface.**
  `CommandOutcome` gains a `verdict` FIELD plus `verified`, reduced by
  `command_verdict` from the run's own status and the journal's own
  verification evidence (a caller passes them as `run_status=` / `evidence=`
  to `command_record`). Previously the headless adapter reduced the
  COMMAND's lifecycle word, so a `/status` against a run whose journal said
  `completed_verified` with clean evidence reported `verdict: "unverified"`,
  `verified: false` — verified live. `verdict`/`verified` are now on the
  TUI and REPL records too, and a command with no run reports the shared
  `NO_RUN_VERDICT` (`"no_run"`). The reduction itself is still
  `cli.runview.run_verdict`, unchanged and still fail-closed.
  (4) **Two registry/validation corrections.** `/watch` was a TUI branch in
  no registry row (ran in one shell, `unknown` in the REPL, invisible to
  the palette and `/help`) and is now a `CommandSpec` with
  `headless: flag-only` -> `neo watch <task-id>`. `/effort` was
  `flag-only` with no `HEADLESS_FLAG_EQUIVALENTS` row, so a headless
  refusal rendered "has a dedicated flag in this surface: " with nothing
  after the colon; it now names `NEO_EFFORT=<level> neo fix ...`, and the
  import-time validator checks BOTH directions of the flag-equivalent
  relationship plus `REQUIRED_COMMANDS ⊆ COMMAND_SPECS`. The validator
  still raises at import, as it always has.
  (5) **An unknown command is a refusal on all three surfaces.** The two
  shells' preflights were gated on `resolution.spec is not None`, and an
  unknown name has `spec is None` by construction, so the TUI recorded
  `ok`/0 for a typo (measured). The branch is now `status != "ok"`, and a
  `None`-spec resolution prints the shared `unknown_command_line(...)`.
  `is_custom_command_line(line, repo_path)` is the guard that keeps
  project/global custom command templates working: an unregistered name is
  a refusal only when no template backs it, on every surface.
  (6) **The TUI no longer publishes another surface's record.**
  `/settings`, `/plugins`, and `/theme` run the REPL handler by design; the
  delegated DECISION (status and exit code) is still shared, but the
  envelope is the running surface's and names its provenance in
  `delegated_from`. A delegated `/theme` also re-adopts the process tokens
  it installed, so `on_unmount` restores the pre-mount theme again (it did
  not for `/theme reset`, whose argument is not a theme name).
  (7) **Verification.** `tests/test_cli_terminal_parity.py` -> 50 passed
  (23 new). Five nonstandard-order shuffles (seeds 7001/7002/7003/9101/
  9102, two with the file order inverted) -> 181 passed each; two more
  with the TUI/UX lane as a pollution canary (4401/4402) -> 252 passed.
  TUI/layout/theme/projection lane -> 464 passed. CLI session/release/
  power-tools lane -> 278 passed. **Real attached PTY, all three surfaces:
  33/33** with the child reporting real stdout AND stderr, plus a real
  non-TTY pipe lane recorded with its own honest `real_terminal: false`.
  Visual evidence 20/20 across three theme profiles with SHA-256 receipts.
  `python -m ruff check cli` clean; `compileall` clean; `git diff --check`
  exit 0 on the four touched source files (the whole tree exits 2 on
  PRE-EXISTING trailing whitespace in three AGENTS.md files).
  **Not run and not claimed: no Docker lane, no live-provider lane, no
  `evals.run`** (no prompt changed). One red is attributed and NOT counted
  as a pass: `test_ceiling03_sessions.py::test_five_thousand_indexed_
  sessions_list_under_100ms_p95` fails only in a combined run in which
  `test_agt_08_effort.py` runs FIRST, at ~152-164 ms against a 100 ms
  budget, and passes 4/4 standalone and with this round's own file in the
  lane; the listing path (`cli/session.py`) is untouched by this round and
  references none of the new functions. `test_ceiling16_surfaces.py::
  TestServePolicies::test_cli_refuses_serve_without_a_model` HANGS on this
  host because `%APPDATA%\neo\settings.toml` declares a model, so its
  "no model configured" precondition is false — proven environmental, not
  this round's code, and the other six tests in that class pass.
- 2026-09-29 (AGT-05 - plan-phase research isolation): **`harness/agent_kernel/
  subagents.py` (NEW public surface), `harness/agent_kernel/strategy.py`,
  `runtime/roles.py`, `harness/config.py`; NEW
  `tests/test_agt_05_plan_isolation.py`; ONE corrected gate in
  `tests/test_ceiling05_knowledge.py`; ONE pre-existing defect fixed in
  `strategy.py`. No Boundary 0-5 signature, no event kind, no serialized
  contract field, no completion status, and no verifier mint changed. Nothing
  here can make `completed_unverified` reachable as success, and nothing here
  replaces verification with prompt shaping: the plan phase is a bounded
  read-only subagent plus a receipt, and the suite pins that the success mint
  still reads `target_passed` / `regression_passed` / `flaky` and that
  `completed_unverified` is still its own status.**
  (1) **The plan phase is STRUCTURAL isolation, not a prompt.** A plan phase
  is worth having only if the exploration it does never lands in the main
  window, and that has to be a property of the architecture. NEW
  `harness.agent_kernel.subagents.PlanResearchSubagent` runs repository
  exploration in its OWN message list and returns a
  `PlanResearchResult` whose type has **no field capable of holding a
  transcript** - only the plan, bounded citations, and counters. What the
  researcher did explore is still reported
  (`transcript_messages` / `transcript_chars` / `transcript_digest`), so the
  isolation is measurable rather than asserted.
  (2) **Plan mode is a CAPABILITY GATE, not an instruction.**
  `PLAN_RESEARCH_CAPABILITIES = ("read", "control")`, and
  `plan_mode_tools()` is DERIVED from the one canonical catalog through
  `harness.agent_kernel.kernel.capability_surface()` - never a hand-written
  list. The researcher's `ToolRegistry` is `restrict()`ed to that set BEFORE
  any handler is installed, so a write is **not a tool the researcher has**:
  `ToolRegistry.validate` raises `unknown tool` before any handler could run.
  `PLAN_RESEARCH_WITHHELD_TOOLS = ("task",)` names the one `control` tool that
  is excluded, because a researcher that can fork work has an unbounded budget
  by construction. `plan_mode_withheld()` returns every withheld capability
  WITH its reason and the tools it carried. `runtime.roles`' `planner` profile
  now derives its `visible_tools` from the SAME function
  (`_planner_tools()` -> `plan_mode_tools()`), so a planning ROLE and a plan
  SUBAGENT cannot disagree about what read-only means.
  (3) **The return is bounded on every axis and says what it dropped.** Turns
  (`plan_research_max_turns`, default 6), tool calls
  (`plan_research_max_tool_calls`, 12), dollars
  (`plan_research_max_cost_usd`, 0.25 - checked BEFORE the next call), the
  returned plan's size (`plan_research_max_chars`, 4000) and the citation
  count (`plan_research_max_citations`, 12). The size cap is a HARD ceiling:
  the omission marker is measured from the real omitted count and the head/tail
  are then sized to fit, so a six-figure omission cannot push the "capped"
  result over the limit. `plan_research_max_chars = 0` means "return nothing",
  which is a real choice and not an accidental "no cap". The receipt names
  which bound stopped the run (`stopped_because`).
  (4) **The architect/coder model split, reported both ways.** `plan_model`
  binds a SECOND `ModelGateway` for the plan request and nothing else, reusing
  the run's own boundary (`call_fn` / `model_client`) exactly as the compaction
  summarizer's gateway does. If that boundary is unreachable the key is **NOT
  honoured** and the receipt names which model actually planned. The researcher's
  spend is folded into the run's own totals by `_absorb_spend`, so a second
  ledger cannot under-report the cost of a plan.
  (5) **The approved plan IS the executed plan, or the run is `blocked`.**
  NEW `DailyCodingStrategy.approve_plan(spec)` compares the digest of the
  approved plan (`RunSpec.metadata["plan_guidance"]`, the path the CLI's
  `/plan` preview already used) against the digest of the block this run
  actually injects. A mismatch returns `completion.blocked(...)` and
  executes nothing - approving plan A and executing plan B is refused, not
  performed quietly. An absent approved plan is NOT inferred as an approval:
  `plan_approval_receipt()` reports `approved: False`. Both receipts ride
  `RunResult.metadata["plan"]` / `["plan_approval"]` and neither carries any
  completion vocabulary.
  (6) **Config discipline: nothing behaviour-changing in `DEFAULTS`.** All
  seven `plan_*` keys are `None` in `harness/config.py`, which is
  behaviour-neutral because an absent key already means "use the internal
  default" and because a value in `DEFAULTS` is merged into every task and
  every eval arm - a truthy `plan_research` would have silently switched every
  run in the project onto a different architecture. `plan_research` is read by
  key presence AND an explicit truthy (`"1"/"true"/"yes"/"on"/"research"`), so a
  typo in a settings file cannot enable it. Every bound is clamped to a floor
  of zero and an unusable value degrades to the default rather than to
  infinity. `tests/test_agt_05_plan_isolation.py::test_the_shipped_default_does_
  not_switch_the_plan_phase_on` fails if any of the seven is given a value.
  (7) **A pre-existing defect this round's tests found, and a VACUOUS gate it
  was hiding.** `DailyCodingStrategy.knowledge()` memoises `False` as its
  "unavailable" sentinel but returned that raw sentinel on the memo-hit path,
  while every caller tests `knowledge is None`. The plan phase calls
  `_compile_knowledge`, so a run with `knowledge_enabled=False` raised
  `'bool' object has no attribute 'compile'` on turn one. Fixed at the ONE
  place that owns the sentinel rather than by re-guarding its three call sites.
  The defect was invisible because
  `tests/test_ceiling05_knowledge.py::test_knowledge_off_arm_never_injects_
  and_never_compiles` was **passing vacuously**: it asserted the `AGENTS.md`
  marker was absent without asserting the run's STATUS, so a run that crashed
  before the model was ever called satisfied it. The marker was also the wrong
  discriminator - `AGENTS.md` is read independently by
  `ContextBuilder.discover_project_instructions`, so it is present in BOTH the
  on and off arms. That test now asserts a real completion (so a crash can
  never satisfy it again) and asserts the absence of the KNOWLEDGE block's own
  header, with a NEW control test proving project instructions still arrive
  without the knowledge compiler.
  Verified: `tests/test_agt_05_plan_isolation.py` -> **47 passed** (host-only;
  no Docker, no provider, no network). Neighbour selections ->
  `test_agent_kernel` + `test_ceiling05_knowledge` + `test_orchestration` +
  `test_tool_protocol` + `test_workspace_security` + `test_capability_surface`
  + `test_config_trace_state` + `test_agt_04_search_budgets` -> **305 passed, 1
  skipped**; `test_agent_loop` + `test_modes` + `test_retrieval_tools` +
  `test_lsp` + `test_ceiling_r2_04_daily_default` -> **214 passed, 1 skipped**;
  `test_evals_run` + `test_evals_tasks` + `test_daily_driver_evals` -> **69
  passed**; `test_stubs_and_deps` + `test_recovery_steering` +
  `test_ceiling_r2_15_trust` + `test_ceiling05_knowledge` +
  `test_difficulty_approval` -> **170 passed**. `python -m evals.run --check` ->
  **14/14 CLEAN**; `python -m evals.run --suite daily-driver --no-docker
  --json` -> **52/52 case arms ok, 0 fail, `zero_false_verified_successes`,
  `zero_unauthorized_mutations`, `zero_lost_edits` all true** - identical to
  the AGT-02/03/04 recorded baseline, which is the measured answer to "does an
  opt-in extra bounded model call perturb a healthy run". Verdict `NOT_READY`
  is reported honestly: the Docker lane, the live-provider lane and the
  sampled manual-repair evidence were **not selected**. `ruff check` and
  `compileall` clean on every owned file. Reverting the one-line
  `knowledge()` fix fails 5 of the new suite's tests, so the regression cannot
  return silently.
- 2026-09-28 (AGT-08 - the effort ladder and the cheap summariser tier):
  **`runtime/model_capabilities.py`, `runtime/model_router.py`,
  `runtime/provider_gateway.py`, `runtime/checkpoint.py`,
  `harness/model_client.py`, `harness/config.py`,
  `harness/agent_kernel/strategy.py`, `harness/agent_kernel/checkpoints.py`,
  `harness/agent_kernel/legacy.py`, `harness/agent_kernel/verified.py`,
  `shared/agent_contracts.py`, `cli/commands.py`, `cli/interactive.py`,
  `cli/tui.py` and `cli/main.py` (ONE additive `--json` key) edited; NEW
  `tests/test_agt_08_effort.py`. No Boundary 0-5 signature, no completion
  status, and no verifier mint changed. Nothing here can make
  `completed_unverified` reachable as success, and nothing here replaces
  verification with prompt shaping: the new code is a model-request parameter
  plus a receipt, and the suite pins that the success mint reads no effort
  setting at any level.**
  (1) **`runtime.model_capabilities` is the ONE effort authority.**
  `EFFORT_LEVELS = (low, medium, high, xhigh, max)` plus `EFFORT_AUTO`;
  `EFFORT_CHOICES`; `EFFORT_ENV_VAR = "NEO_EFFORT"`; the closed
  `EFFORT_STATUSES`; `EffortKnob` (a provider family's REAL parameter plus the
  levels it accepts); `EffortPlan` (the honest answer, whose `parameters`
  property is the ONLY thing a caller may merge and is EMPTY for every status
  except `sent`); `normalize_effort`, `effort_from_env`, `resolve_effort`,
  `map_effort`, `effort_family_for`, `synthetic_effort_plan`, and an
  operator-extensible `register_effort_knob` / `unregister_effort_knob` /
  `reset_effort_knobs` / `known_effort_knobs`. Built-in knobs:
  `openai -> reasoning_effort` (three levels), `anthropic -> thinking`
  (`{"type": "enabled", "budget_tokens": N}`), `google -> thinking_budget`.
  Three rules: a level the family does not accept is `unsupported_level` and is
  **never clamped** to the nearest rung it does accept; a model with no
  declared knob is `unsupported_model`; an unrecognised value is `invalid`
  with the value echoed. A malformed knob declaration RAISES
  (`CapabilityError`), the same rule as `register_capability`. The MODEL
  decides the family, not the provider name, because an `openai`-compatible
  gateway fronts Claude and Gemini too.
  (2) **`call_model` gained one additive keyword, `effort`.** The plan is
  resolved against the FINAL target (after any capability screen may have
  re-pointed it) and merged into the request kwargs only when `plan.sent`.
  Every ledger row, every `get_last_usage()` record and every `model_routed`
  trace event now carries `effort`, `effort_status`, `effort_sent`,
  `effort_parameter`, `effort_value`, `effort_model` and `effort_detail`,
  including a FAILED attempt, because a cost claim about a failed call needs
  the same explanation. The resilient pipeline
  (`runtime.provider_gateway.resilient_call_model`) gained a keyword-only
  `effort` and resolves it **per candidate**: a fallback target may support a
  different knob, and a plan computed against the primary would be a lie for
  the model that actually answered.
  (3) **`harness.model_client.ModelClient.call` / `call_structured` gained
  `effort`.** Forwarded only when the boundary EXPLICITLY NAMES the parameter
  (`_boundary_names`), the same discipline `_boundary_streams` uses for
  `stream`: a `**kwargs` double accepts a keyword it then hands to something
  that rejects it, which is a run-killing `TypeError` rather than a degraded
  feature. The per-call record always carries `effort` plus the router's
  `effort_*` fields. The real boundary, `runtime.model_router.call_model`,
  names it, so production is unaffected. **`cli/main.py::_result_json` gained
  one additive `effort` key** backed by `_effort_json`: the historical document
  published only `len(model_calls)`, which made "ran at high" and "asked for
  high and the provider ignored it" the same bytes. It carries the level, the
  per-call statuses and parameters, a `supported` boolean, and up to
  `EFFORT_RECEIPT_LIMIT` (50) per-call receipts with a `receipts_truncated`
  count - bounded so a 400-call run cannot turn a result document into a log.
  It reads only what `TaskResult.model_calls` already carries and never
  raises: a run whose boundary knew nothing about effort reports
  `supported: false` with an empty list rather than a missing key.
  (4) **The effort rung is part of the resume identity.**
  `runtime.checkpoint.effort_identity` and
  `harness.agent_kernel.checkpoints.effort_identity_for` resolve it through
  the same authority (so `hi` and `high` are one identity), and the rung
  joins `_IDENTITY_FIELDS` on both paths plus the additive
  `shared.agent_contracts.Checkpoint.effort_identity` field. A mismatch takes
  the existing fail-closed path - a fresh attempt, journalled - because
  resuming a high-effort run at low effort is a lie about the run. The rung
  travels from the run's config to a `RunSpec`'s metadata through the one seam
  `harness.agent_kernel.checkpoints.with_effort`.
  (5) **Compaction defaults to the cheap tier.** New `context_compaction_tier`
  (`cheap` | `expensive` | `run`, default `cheap`) in `harness/config.py`;
  `DailyCodingStrategy._summary_gateway` prefers the run's `model_tiers["easy"]`
  model (falling back to the runtime's own default easy tier) for the
  summariser, and the existing `context_compaction_model` still wins outright.
  The `context_compacted` receipt, the `compactions.jsonl` row and the journal
  event all gain `compaction_tier`, `compaction_tier_configured`,
  `compaction_tier_model` and `compaction_tier_honoured`; the pre-existing
  `compaction_model*` fields are unchanged in meaning. A cheap tier that IS the
  run's own model returns the run's own gateway, byte-identical to the
  pre-round path. An unrecognised tier falls back to `cheap`, because a typo
  must not make summarisation cost frontier prices.
  (6) **`/effort` is a real command, and it is discoverable.** One
  `CommandSpec` row in `cli/commands.py` (alias `/thinking`,
  `in_flight_policy="allow"` - effort is the control a person reaches for when
  a run is struggling) plus the public pure helpers `EFFORT_LADDER`,
  `effort_levels`, `effort_receipt`, `apply_effort` and `render_effort`. The
  composer hint, the palette, the headless policy table and `/help` are all
  projections of the registry, so `/eff` teaches the whole ladder while it is
  being typed. `apply_effort` writes BOTH `state["effort"]` and `NEO_EFFORT`,
  because the router resolves the environment when a run's config is assembled
  by a path the command does not own (a worker subprocess, a mode module, the
  headless runner); writing only the session key would leave exactly the
  "set to high that silently does nothing" failure the ladder exists to remove.
  (7) **Two new `harness/config.py` `DEFAULTS` entries: `effort: "auto"` and
  `effort_parameter: None`, plus `context_compaction_tier: "cheap"`.** `auto`
  is behaviour-NEUTRAL (no parameter is sent), which is the only reason it is
  safe to publish in a table merged into every task and every eval arm; a test
  pins that the auto request kwargs are byte-identical. `get_config` is the
  ONE env-aware merge: `Task.config["effort"]` > `NEO_EFFORT` > `auto`,
  checked against the CALLER's dict because the merged dict always carries the
  default.

- 2026-09-28 (AGT-07 — two-axis safety and an approver agent): **`execution/
  sandbox.py`, `harness/agent_kernel/policy.py`, `shared/approval.py` and
  `harness/config.py` edited; NEW `harness/approver.py` and NEW
  `tests/test_agt_07_two_axis_approver.py`. No Boundary 0-5 signature, no
  completion status, and no verifier mint changed: nothing here can make
  `completed_unverified` reachable as success, and no technique here replaces
  verification with prompt shaping. `PolicyEngine.evaluate` is byte-identical;
  every new entry point is additive and keyword-only.**
  (1) **Axis (a) — technical containment is now a declared, reported object.**
  `execution.sandbox.ContainmentPolicy` (+ `resolve_containment`,
  `readonly_overlays`, `writable_overlays`, `containment_receipt`) answers "what
  does the sandbox permit" with no reference to prompting. Network is OFF unless
  a declaration (`allow_network=True` / a policy) turns it on, and the receipt
  names the declaration plus the egress allowlist. `DEFAULT_READONLY_SUBPATHS`
  (`.git`, `.hg`, `.svn`, `.bzr`, `_darcs`, `.neo`) are mounted READ-ONLY
  *inside* the writable workspace by re-mounting each existing directory after
  the workspace bind mount, so "the workspace is writable" no longer implies
  "the repository is writable". Declaring `sandbox_writable_roots` makes the
  workspace mount itself `:ro` and mounts exactly those subtrees `:rw`. A
  declared writable root may NEVER overlap a read-only path (the conflict is
  recorded and the read-only side wins), an unusable path (absolute, `..`, empty)
  is dropped with a note rather than clamped, and a read-only path that does not
  exist on the host is REPORTED absent and NOT mounted — `docker run -v` would
  create it, inside a user's repository.
  (2) **`assert_sandbox_argv_isolated` gained `*, writable_paths=()` and is
  STRICTER, not looser.** A mount whose source is strictly inside the workspace
  is now permitted (it reaches nothing the existing root mount does not), and it
  must declare its mode explicitly — because `docker -v src:dst` defaults to
  READ-WRITE, a containment overlay that lost its `:ro` would silently *widen*
  the container, and this gate is the thing that is supposed to catch exactly
  that. A read-write in-workspace mount is admitted only where a writable root
  was declared. The two-argument call is unchanged.
  (3) **Axis (b) reporting beside axis (a).** `harness.agent_kernel.policy`
  gained `ContainmentAxis`, `SafetyReceipt`, and
  `PolicyEngine.evaluate_axes(call, *, containment=None,
  containment_known=True, context=None)` / `.privileged_receipt(...)`, which
  delegate to `evaluate` and wrap its decision. `SafetyReceipt.from_decision_only`
  is the only constructor that omits a containment receipt and it produces
  `ContainmentAxis.unknown()`; `ContainmentAxis.contained` is False while the
  axis is unknown; `requires_human` is True exactly for an `ask`; and
  `SafetyReceipt.axes_confused()` reports a receipt whose containment half
  carries a prompting field or whose decision half carries a sandbox field, so
  a future edit that merges the axes fails a test instead of shipping a lie.
  `shared.approval.describe_axes(...)` owns the one-line rendering, and it is
  phrased so neither half can imply the other.
  (4) **NEW `harness/approver.py`.** `ApproverAgent` (+
  `render_approver_prompt`, `review_privileged_action`, `journal_path_for`,
  `append_journal`, `ApproverJournalEntry`) judges ONLY actions that already
  require a human: `shared.approval.approver_admissible` is consulted before a
  prompt is built, so an unprivileged action never reaches a model at all
  (`not_privileged`). It is a second, cheap opinion — `difficulty_hint="easy"`,
  never a hardcoded model name — and it can refuse; it cannot create, widen or
  silently satisfy a request. It is fail-closed by construction with NO key that
  turns that off: unparseable / ambiguous / malformed / reason-less replies,
  a timeout, a provider exception, a disabled agent, and an untraceable
  subagent escalation are all denials, each with a distinct `failure` from the
  closed `APPROVER_FAILURES` set so a refusal-with-a-reason stays
  distinguishable from a refusal we could not obtain. Every prompt, verdict and
  journal row carries an `ApprovalOrigin` (thread / subagent / parent), so a
  subagent's escalation is labelled as that subagent's and an untraceable one is
  refused outright.
  (5) **The reply contract lives in `shared/approval.py` (section 5), not in the
  model layer.** `parse_approver_reply` is the ONE parser: a reply is an approval
  only if it names a decision in the approve set AND states a non-empty reason;
  JSON (or fenced JSON, or `APPROVE: …`) and a first-word text form are both
  understood, and an unknown token, two disagreeing keys, or both directions in
  one reply all deny. `ApproverReply.approved` is True only for a parsed, reasoned
  approval, so a caller that ignores `failure` still denies. `SAFETY_AXES`,
  `CONTAINMENT_AXIS`, `DECISION_AXIS`, `ApprovalOrigin`, `ApproverRequest`,
  `approver_admissible` and `describe_axes` are additive exports; sections 1-4
  are unchanged, same signatures, same output.
  (6) **Config: six `None` keys in `DEFAULTS`** — `approver_agent` (master
  switch, read by key presence + truthiness), `approver_model`,
  `approver_timeout_s`, `approver_max_chars`, `sandbox_readonly_paths`,
  `sandbox_writable_roots`. `None` is behaviour-neutral for the four the
  approver reads (an absent key already meant "no approver model"), and
  `sandbox_readonly_paths=None` means "keep `DEFAULT_READONLY_SUBPATHS`" while
  an explicit `[]` is a genuine opt-out — which is why the two are different
  values. The containment default itself lives in `execution/sandbox.py`, NOT in
  `DEFAULTS`, so publishing it there could not silently switch every task and
  every eval arm.
  (7) **Not wired — filed requests, not passes.** `harness/approver.py` has no
  production call site: the approval prompts in `cli/interactive.py`,
  `cli/tui.py`, `harness/agent_loop.py` and `runtime/approval.py` are
  unchanged, because a cheap model must sit in FRONT of a human prompt and each
  of those decides where a prompt lives. `execution/warm_sandbox.py` still
  builds its argv without the read-only overlays (it is not in this round's
  file list), so a warm-container task does not get the `.git` boundary.
  Verified: NEW `tests/test_agt_07_two_axis_approver.py` → **37 passed**
  (34 host-only + 3 real-Docker; the Docker proofs run a real container and read
  the read-only `.git` off it, and one of them is the control that proves
  `readonly_paths=[]` really is an opt-out). `tests/test_sandbox.py
  tests/test_ceiling_security.py tests/test_workspace_security.py` → **154
  passed**; `tests/test_agent_kernel.py tests/test_tool_protocol.py
  tests/test_modes.py` → **175 passed**; `tests/test_ceiling_r2_15_trust.py
  tests/test_difficulty_approval.py tests/test_config_trace_state.py` → **79
  passed**; `tests/test_ceiling_r2_17_daily_truth.py tests/test_agent_loop.py
  tests/test_recovery_steering.py` → **197 passed**; `tests/test_verify.py
  tests/test_verify_js.py` → **68 passed** (real Docker);
  `tests/test_e2e_run_task.py` → **28 passed** (real Docker);
  `python -m evals.run --check` → **14/14 CLEAN**. **No live-provider lane was
  run and none is claimed** — every model call in the new suite is a scripted
  double.
- 2026-09-28 (AGT-02 — bounded reflection loop): **`harness/tool_errors.py`,
  `harness/agent_loop.py` and `harness/config.py` edited; NEW
  `tests/test_agt_02_reflection.py`. No Boundary 0-5 signature, completion
  status, serialized field, or verifier mint changed, and the mint condition
  (`target_test_passed and regression_passed and not flaky`) is untouched — an
  exhausted reflection budget ends a run as `failed`, never as success.**
  (1) **One loop, two caps, both configurable and both reported.** The legacy
  agent loop routes EVERY failure it sees (tool-call parse error, failed tool
  call, failed VERIFY, approval refusal, forbidden-path refusal, doom-loop
  stop, exhausted provider retry) through ONE seam, `harness.tool_errors.
  ReflectionBudget`, and the failure becomes the NEXT USER MESSAGE verbatim with
  its evidence attached. `reflection_max_per_step` (3) bounds CONSECUTIVE
  failures — a successful turn ends the streak; `reflection_max_per_run` (12)
  bounds the whole run. Both are in `DEFAULTS` (deliberately behaviour-changing:
  an unbounded reflection loop is the defect), both are in the new `reflection`
  key of the existing `recovery_stats` journal row, and both ride the result as
  `AgentResult["reflection"]` plus `AgentResult["end_reason"]` (additive keys).
  A run that spends its budget ends with a reason naming the cap and the last
  failure; it never retries silently and never stops without one.
  (2) **One classification, and it is derived.** `harness.tool_errors.
  retry_class_of(kind)` projects an ALREADY-classified kind onto the retry
  axis: `transient_provider` (the provider's own retryable set),
  `model_error` (`malformed_tool_call`), `task_error` (every other `POLICY`
  row), `policy_refusal` (the rows whose `POLICY` action is `avoid_shape` /
  `forbid_path`) and `terminal` (environment faults, terminal model failures,
  `loop_detected`). There is NO second error table: the inputs are the existing
  `POLICY` keys and the existing `classify_model_failure` kinds, and a refusal
  is read off the one table's own action slug. An unrecognised kind is
  `task_error` (reflective), never a refusal, because guessing a refusal would
  silently disable recovery for an unclassified failure.
  (3) **A refusal costs nothing and cannot be laundered.** A non-retryable
  failure is journalled with `charged=False`, leaves both counters untouched,
  and its rendered message states that the decision STANDS — it never says "try
  again" and never offers to rephrase the refused call. So a refused command
  cannot be used to exhaust a run's recovery budget, and a transient provider
  fault (which IS retryable) does spend it. Pinned both ways.
  (4) **One behavioural correction found while building this, and it is
  load-bearing.** The legacy live-BASH path treated a NON-ZERO EXIT as a failed
  call, so `python -m pytest` reporting failures was a "tool error" — a class
  that a reflection loop must spend budget on. A command that RAN and reported
  a non-zero status is a RESULT, so `_run_bash_live` now keeps the historical
  `exit=N` shape byte-for-byte and reserves `ok=False` for a call that did not
  produce a result (the deny-guard, a bad path, a refused edit, a raised
  harness exception). A TIMEOUT is the one non-zero exit that is an
  infrastructure limit rather than a result, and it is now shaped through the
  shared `shape_tool_output` and reported as a failed call. (5) **New journal
  event `reflection`** carries `kind`, `retry_class`, `evidence` (bounded with
  the shared head+tail omission marker), `charged`/`allowed`/`exhausted`,
  `reason`, and both caps — so a run's struggle is reconstructable from the
  trace alone. `cli/runview.py` does not yet classify `reflection` (nor
  `tool_recovery`, `command_recovery`, `command_refused`, `loop_guard` or
  `recovery_stats`, which the legacy loop already emitted); see the
  cross-terminal request in `harness/AGENTS.md`.
  Verified: `tests/test_agt_02_reflection.py` -> **22 passed**;
  `tests/test_agent_loop.py` + `test_recovery_steering.py` + `test_tool_errors.py`
  + `test_config_trace_state.py` -> **169 passed**;
  `test_agent_kernel` + `test_modes` + `test_ceiling_r2_04_daily_default` +
  `test_cli_runview` + `test_cli_tracelog` -> **307 passed**;
  `test_ceiling14_resilience` + `test_steering` + `test_workspace_security` +
  `test_tool_protocol` -> **192 passed**; `python -m evals.run --check` ->
  **14/14 CLEAN**; `python -m evals.run --suite daily-driver --no-docker
  --json` -> **52/52 case arms, 28/28 feature-evidence arms,
  zero_false_verified_successes, zero_lost_edits, zero_unauthorized_mutations,
  cost $0.0246** — byte-identical to the recorded pre-round baseline.
  **No Docker lane and no live-provider lane were run** and neither is claimed.
- 2026-09-28 (AGT-04 — search budgets, concurrency classes, doom loop):
  **`harness/retrieval.py`, `harness/tools.py`,
  `harness/agent_kernel/strategy.py`, `harness/agent_kernel/budget.py` and
  `harness/agent_kernel/tools.py` edited; `harness/config.py` gained seven
  defaulted keys. No Boundary 0-5 signature, completion status, event-kind
  vocabulary, or verifier mint changed, and the mint condition
  (`target_test_passed and regression_passed and not flaky`) is untouched.**
  (1) **Search caps ERROR; there is no page two.** `harness.retrieval.search_repo`
  is the one bounded search. Above `search_max_matches` (default 50) it returns
  `SearchOutcome(ok=False, error="too_many_matches")` carrying the count (a
  lower bound, because the scan stops at cap+1), the distinct file count, and at
  most `search_sample_matches` (default 10) sample lines — never the match set.
  It is bounded on three axes: matches, files (`search_max_files_scanned`,
  default 5000), and rendered bytes. The paging-shaped argument names are the
  CLOSED set `harness.retrieval.PAGING_ARGUMENTS` (28 names); a paging
  argument is refused with `error="paging_refused"` rather than honoured, and
  `harness.retrieval.paging_affordances()` audits the live catalog against the
  set. Rationale, measured: offering paging scores WORSE than offering no
  search, because a model pages exhaustively until the cap stops it.
  `start_line`/`end_line` are deliberately NOT paging (they bound a line window
  of one file, i.e. a narrowing). (2) **Concurrency is declared on the catalog
  entry.** `harness.tools.ToolSpec` gains `read_only: bool | None`; `None`
  derives from `side_effect_class == "read_only"`, and every read-only catalog
  entry declares it explicitly (16 of 45). `read_only` is part of
  `_catalog_shape`, so it is inside `catalog_fingerprint()` and inside
  `catalog_parity_report()` — a catalog that agrees on names and schemas but
  disagrees on parallelism is DIVERGENT, not identical. `harness.tools.
  catalog_concurrency_report()` renders the two classes.
  `ToolRegistry.is_concurrent(call) -> bool` is the boolean form;
  `ToolRegistry.concurrency_class(call) -> "concurrent"|"sequential"` is the
  reporting form. **The dispatcher must use the boolean one** — see (3).
  (3) **A fan-out is bounded in both directions and splits by class.**
  `harness.agent_kernel.budget.plan_fanout` returns a `FanoutPlan`
  (`concurrent` / `sequential` / `refused`); `max_tool_fanout` (default 8)
  bounds the TOTAL calls a turn may execute, spent read-only first, and an
  over-budget call is refused with a reason (`error_kind="fanout_exceeded"`),
  never dropped or deferred. `bound_fanout_output` bounds the COMBINED return
  (`max_tool_fanout_chars`, default 24000), replacing over-budget results with a
  bounded note. `plan_fanout` reads its predicate STRICTLY — only `True` or the
  literal `"concurrent"` counts — because `bool("sequential")` is `True` and
  stringly-typed scheduling silently runs mutations as if they were reads.
  (4) **A repeated identical call is a DECISION requiring the user.**
  `ToolLoopGuard`'s default bound is 3 (`max_repeat_tool_calls`).
  `DailyCodingStrategy._doom_loop_decision` runs BEFORE any dispatch and, when
  the guard fires, returns `CompletionPolicy.needs_input` and emits a
  `doom_loop_detected` event; nothing from that turn executes. The pre-flight
  RECORDS the observation (`ToolRegistry.note_repeat -> (blocked, count,
  fingerprint)`) and passes the fingerprints in the dispatch context as
  `loop_guard_pre_checked`, which the registry's own `loop_detected` refusal
  skips — one observation per call, or the bound arrives at half the turns its
  receipt names. The registry keeps that refusal as the last line for callers
  that dispatch directly. Read-only tools stay exempt by default
  (`loop_guard_read_only`), a measured policy, not an oversight.
  (5) **Tool output is capped at STORAGE time.**
  `harness.agent_kernel.budget.cap_tool_output` caps each result in TOKENS
  (`tool_output_token_limit`, default 4000; `0` disables) and is applied in
  `strategy._execute_calls` BEFORE the conversation, journal, trace row, or next
  model request sees it, emitting a `tool_output_capped` receipt. Head kept, tail
  dropped, `[neo-truncated: N of M chars omitted]` marker always present. The
  kernel's `grep` and `glob` handlers now route through `search_repo` instead of
  hand-rolled scans. New trace events (additive, safe to surface):
  `doom_loop_detected`, `tool_output_capped`, `tool_fanout`,
  `tool_concurrency`. Verified: `tests/test_agt_04_search_budgets.py` ->
  **40 passed, 1 skipped** (host-only); the kernel/tool/retrieval/config/
  recovery/knowledge/workspace/codemod selection -> **403 passed, 3 skipped**;
  `evals.run --check` -> **14/14 CLEAN**; `ruff check` clean on all seven
  files. **No Docker lane and no live-provider lane were run.**

- 2026-09-28 (AGT-03 — lint inside the edit, before the write is committed):
  **`harness/lint.py`, `harness/editor.py`, `harness/tools.py` edited; NEW
  `tests/test_agt_03_edit_lint.py`. No Boundary 0-5 signature, completion
  status, event-kind vocabulary, or verifier mint changed, and the mint
  condition (`target_test_passed and regression_passed and not flaky`) is
  untouched — the gate refuses MUTATIONS and has no say in whether a run
  completed. The loop's existing post-edit lint gate is KEPT as the backstop;
  the in-edit check is a second layer in front of it, not a replacement.**
  (1) **The check is pre-commit, so a broken edit is discarded, not
  undone.** `harness.editor.apply_text_edit` computes the post-image bytes as
  a splice, so at the moment the old code wrote them it already had them in
  memory. It now checks THEM (`harness.editor.precommit_check` ->
  `harness.lint.check_source_for_edit`) and refuses BEFORE
  `execution.workspace._atomic_write_bytes`. A caller-supplied `validate`
  callable keeps its post-write position and its rollback, because its
  `(root, [paths])` signature reads the tree; that is the only path that still
  rolls back. `EditOutcome` gains five additive receipt fields
  (`pre_commit`, `check_status`, `check_line`, `check_context`,
  `check_reason`) and `write_mode` gains `pre_commit_refused`. `rolled_back`
  keeps its documented meaning — the file on disk is byte-identical to its
  pre-edit state — and `pre_commit` is what distinguishes "never written" from
  "written and restored". (2) **The status is TRI-STATE, and only one value
  means "checked".** `harness.lint.CHECK_STATUSES` is the closed set
  `passed | failed | unchecked | disabled`. An unsupported language, a missing
  tree-sitter grammar, non-text content and bytes that do not decode are
  `unchecked` WITH a reason; `harness.tools` adds a fifth,
  `not_applicable`, for a mutation with no candidate content (a read-only
  call, `delete`/`undo`, a write the backend will refuse, arguments the gate
  cannot reconstruct). A successful edit whose file had no checker says
  `[in-edit lint: unchecked — …]` in its own tool result: a silent skip reads
  as a pass. `harness.lint.EditCheck.checked` is the boolean a consumer must
  read before claiming anything was verified. (3) **The refusal carries ±3
  lines of context** (`harness.lint.DEFAULT_CONTEXT_LINES`, overridable),
  rendered with real line numbers and the offending line marked, per-line
  truncated at 200 chars and capped at 1200 chars with an explicit
  `[... context truncated ...]`. "syntax error at line 2" is not a form a
  model can self-correct from. (4) **Every mutating tool is screened, not just
  `edit`.** `harness.tools.MUTATING_TOOLS` is the closed list
  (`edit`, `write`, `apply_patch`, `rename`, `delete`, `undo`,
  `rename_symbol`, `update_signature`) and `harness.tools.
  precommit_check_call(root, tool, arguments, config=)` returns one JSON-safe
  receipt that ALWAYS carries a `status` and a `refused` boolean; the
  per-file form is `precommit_candidates`, and a multi-file `apply_patch`
  cannot hide a broken file behind a clean one (any failure refuses, and a
  partially-covered mutation reports the WEAKEST status any candidate had
  rather than collapsing to `passed`). `TypedToolRuntime.execute` consults the
  gate and returns `execution.workspace.ToolResult(ok=False, …)` — a VALUE,
  never an exception, and the backend is never reached. Candidate content is
  read as STRICT utf-8; a non-UTF-8 file is reported undecoded rather than
  checked as mojibake. (5) **Config: three keys, none of them in `DEFAULTS`.**
  `edit_inline_lint` (default on; only an explicit `False` disables — an
  absent key and `None` both mean on), `edit_inline_lint_names` (default
  OFF — the undefined-name pass is false-positive-prone and a false positive
  here refuses a mutation outright, while the loop gate runs the same pass
  moments later for the cost of one attempt), `edit_inline_lint_context_lines`
  (default 3). A default in `DEFAULTS` is merged into every task and every
  eval arm, so publishing one would silently switch every run;
  `test_no_in_edit_lint_key_is_in_the_harness_defaults` fails if one appears.
  (6) **One incidental fix, disclosed.** `harness.lint.check_syntax` used to
  `compile()` every non-JS/TS file, so a changed `.txt`/`.cfg`/`.rst` file with
  prose in it produced a bogus "syntax error" finding from the LOOP gate. The
  suffix->language decision is now the single `LANGUAGE_BY_SUFFIX` table, so
  an unrecognised extension is ignored — which can only ever REMOVE a false
  refusal, never a real finding. The pre-existing RUF022/B905 findings in that
  file are the ratchet's, unchanged. Verified: `tests/test_agt_03_edit_lint.py`
  -> **35 passed**; `test_ceiling_r2_06_editing` -> **34 passed** (its atomic-
  write test was made STRONGER: it now pins zero write calls for a pre-commit
  refusal AND the two-call rollback for a caller-supplied `validate`, where it
  previously pinned the two calls for a default syntax failure);
  `test_ceiling_r2_07_codemod` -> **83 passed**; `test_batch_docs_lint` +
  `test_multilang_graph` -> **66 passed**; `test_tool_protocol` +
  `test_editor_prompts` + `test_adversarial` + `test_workspace_security` ->
  **148 passed, 1 skipped**; `test_agent_kernel` + `test_config_trace_state`
  -> **61 passed**; `test_agent_loop` -> **62 passed**;
  `test_ceiling_r2_03_config_guard` + `test_ceiling_r2_12_polyglot` +
  `test_coordination` -> **188 passed, 1 skipped**; `test_evals_run` +
  `test_evals_tasks` + `test_daily_driver_evals` -> **69 passed**; `evals.run
  --check` -> **14/14 CLEAN**; `evals.run --suite daily-driver --no-docker
  --json` -> **52/52 case arms, 26/26 valid comparisons, 28/28
  feature-evidence arms, 0 failures, `zero_false_verified_successes=true`,
  `zero_lost_edits=true`, cost $0.0246** — byte-identical to the AGT-02 /
  AGT-04 recorded baseline. `ruff check` is within the ratchet's existing
  allowance (the 2 findings left in `harness/lint.py` are pre-existing and
  recorded in `scripts/lint-baseline.txt`).
  **NOT yet on the production mutation path, and pinned as such:**
  `execution/workspace.py::SafeToolBackend.execute` (the typed kernel's
  dispatcher, and therefore also `harness/codemod.py::apply_plan`, which
  dispatches every replacement through it) and `harness/agent_loop.py`'s
  legacy EDIT/WRITE verbs do NOT call the gate. Both keep their own
  post-write protection, so nothing broken lands either way; the wiring is one
  call in each file and is filed in `harness/AGENTS.md`. The two
  self-arming pins are
  `test_the_codemod_path_does_not_yet_use_the_in_edit_gate` and
  `test_the_workspace_backend_owner_has_a_one_call_hook_to_wire`, which FAIL
  the moment either file is wired. A third pin,
  `test_the_facade_resolves_a_real_backend_root_and_refuses_before_the_write`,
  drives the real `execution.workspace.SafeToolBackend` and proves the
  root-resolution helper reads the attribute that object actually has
  (`workspace.root`) and that the gate refuses before the write. Daily-driver
  readiness is reported `NOT_READY` because the Docker, live-provider and
  sampled manual-repair lanes were not selected. **No Docker lane and no
  live-provider lane were run** and neither is claimed.

- 2026-09-26 (R2-16 — extension and operations wiring; the security surface
  stops being decorative): **NEW `cli/migration.py`; `cli/connectors.py`,
  `cli/plugins.py`, `cli/doctor.py`, `extensions/user_hooks.py`,
  `cli/main.py` (additive subcommands only) edited. No Boundary 0-5
  signature, event kind, serialized field, completion status, or verifier mint
  changed, and `harness/config.py`'s `DEFAULTS` gained nothing.** The
  connector return shapes KEPT their historical keys and gained additive ones;
  `install_from_local` gained two keyword-only defaults; `mcp_server`,
  `integrations`, `execution`, `runtime`, `harness`, and `shared` were not
  edited.
  (1) **`mcp_server/namespace.py` is now reached by a real call, or the test
  fails.** `cli/connectors.py::call_tool` is the pre-call boundary: it
  evaluates the `PreToolUse` user-hook gate, fetches the LIVE catalog,
  authorizes least privilege against the connector's declaration, re-hashes
  the live catalog against any recorded approval pins, dispatches, and reviews
  the RESULT as untrusted content. `list_tools` returns the namespaced
  `mcp__<server>__<tool>` catalog and, when a declaration exists, the visible
  list IS the callable list (`blocked` carries the refused tools separately).
  `test_the_mcp_namespace_layer_is_reached_by_a_real_call` drives a REAL
  `python -m mcp_server` subprocess over stdio and asserts the receipt names
  the namespaced id, the 64-hex definition digest, and the untrusted review —
  so the layer can rot again only if that test rots. Least privilege runs
  BEFORE the pin recheck (an unauthorized tool is refused as unauthorized, not
  as a pin mismatch), and that order is test-pinned.
  (2) **NEW connector permission schema — the blast radius is a statement.**
  `[connector_permissions.<label>]` in
  `<repo>/.neo/connector-permissions.toml` (project),
  `<repo>/.neo/connector-permissions.local.toml` (local), or
  `<global config dir>/connector-permissions.toml` (global). The closed key set
  is `tools`, `side_effect`, `network`, `write`, `write_paths`, `pins`, `note`;
  an UNKNOWN KEY RAISES (`ConnectorError`), because a typo in a security
  declaration must not read as "nothing was declared". `tools` is the
  callable allowlist (`"*"` is the operator's explicit "whatever this server
  declares"); `side_effect` is the session ceiling on each tool's DECLARED
  class using the `mcp_server.namespace` vocabulary
  `read < search < network < mutation < destructive`; `network` is a host
  allowlist checked against BOTH the declared list and
  `shared.egress.egress_decision`; `write` is a key-PRESENCE tri-state where
  ABSENT means "the operator never said" and is reported as
  `write_declared: false` without being enforced — the only
  backwards-compatible reading. `neo mcp add` now DECLARES every connector it
  creates, with values that reproduce the pre-declaration behaviour exactly
  (`tools = ["*"]`, `side_effect = "mutation"`, no network hosts, `write`
  undeclared), so adding a connector can never silently break an existing
  script while making the declaration visible in a file. `neo mcp
  permissions <label> [--tool …] [--side-effect …] [--network …]
  [--write|--no-write] [--clear] [--json]` is the writable surface. New
  return keys on `list_tools`/`call_tool`: `namespaced`, `blocked`, `receipt`
  (`ConnectorReceipt`); `list_servers` rows and `check_health` rows gain
  `permissions_declared`/`permissions`/`blocked_tools`. An UNDECLARED
  connector is not enforced and every surface says so
  (`enforced: false`, "the call was NOT gated") — that limitation is
  surfaced, not hidden.
  (3) **`extensions/user_hooks.py` — the fail policy is per EVENT CLASS,
  stated, and overridable.** New `HOOK_FAIL_POLICIES` (table),
  `HOOK_FAIL_POLICY_REASONS` (why, per event), `FAIL_POLICIES` /
  `FAIL_POLICY_VALUES` (closed vocabulary), `HookGate`,
  `HookEngine.gate(event, subject)`, `HookConfig.fail_policy_for(event,
  spec)`, and `HookSpec.fail_policy`. Default: `PreToolUse` and `Stop` are
  **fail-closed** (a gate that could not run has not approved anything);
  `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `SessionStart` and
  `SessionEnd` are **fail-open** (observational/lifecycle — failing closed
  would only turn a red hook into a red run). Overrides: a tier's
  `"fail_policy": {"<Event>": …}` table, a tier's `default_fail_policy`, and a
  per-hook `fail_policy`; the winning source is reported on every gate
  (`table` / `config:event` / `config:default` / `hook:<id>`). An OBSERVATIONAL
  event can never refuse even if its policy is `fail_closed` — the structural
  "observational cannot decide" rule is not made conditional. A handler that
  raises, a missing executable (127), a spawn failure (126), a timeout (124),
  unusable output, and a spent latency budget are all FAILURES, so the policy
  governs them. `CompletionGate` now routes through `gate()`, which means a
  broken `Stop` hook downgrades `completed_verified` →
  `completed_unverified` with `blocked_by="fail_policy"`; it still only ever
  downgrades, and `CompletionGateVerdict` gained additive
  `fail_policy` / `fail_policy_source`. NEW CLI `neo hooks list|run` is the
  operator surface for both.
  (4) **The hook layer is wired at a real tool-call path this terminal owns.**
  `cli/connectors.py` fires `SessionStart` (once per label per process),
  `PreToolUse` BEFORE any server is spawned, then `PostToolUse` /
  `PostToolUseFailure`, and every dispatch's receipt rides on
  `ConnectorReceipt.hooks`. A blocking or fail-closed `PreToolUse` refuses
   the call and the server is never launched. If the `extensions` package is
   not importable (a broken install) or a hook config is
   unparseable, the connector surface degrades to "no hook layer" and says so
   in the receipt as `hooks_unavailable` — an unusable hook LAYER never becomes
   a crash on the connector path, and never becomes silent protection.
   (`extensions` IS already in `[tool.setuptools] packages`, so this fallback
   is defensive rather than a packaging gap.)

  (5) **NEW `cli/migration.py` — `neo migrate`.** Six detectors, each a pure
  predicate: `legacy_config_toml` (the real `~/.neo/config.toml` →
  two-tier `settings.toml` chain, renaming the legacy file to
  `config.toml.migrated-v1` rather than deleting it),
  `hook_config_schema_version`, `plugin_install_receipt` (backfill for a
  pre-atomic install, honestly sourced as `source_kind="backfill"`),
  `plugin_install_residue` (DESTRUCTIVE: reclaim `.staging-*`/`.trash-*` from
  an interrupted install), `connector_permission_declaration` (declare the
  default set for every pre-existing connector), `in_repo_log_root` (the
  run-artifact root is inside the repository; ensures the `.gitignore` entry
  and NAMES the relocation command, but never moves the bytes itself).
  `plan_migrations()` is READ-ONLY and every detector reports — including the
  ones that do not apply and why, so "nothing to do" is distinguishable from
  "the detector could not look". `apply_migrations()` runs the whole plan as
  ONE `_Transaction` that snapshots previous bytes before every write and MOVES
  a removal target aside instead of deleting it, so a failure anywhere rolls
  the whole run back and a destructive step is as reversible as a write until
  the commit. `MigrationResult.applied` is False after a rollback (a rolled-back
  run applied nothing, and the field means "changes are in place").
  `MAX_ACTIONS_PER_RUN = 500` is a reported safety bound, not a silent cap. A
  receipt is written to `<neo_home>/migrations/migrate-<ts>-<pid>.json` with
  `schema_version = 1`. CLI: `neo migrate [--repo] [--log-root] [--apply]
  [--yes] [--allow-destructive] [--only ID] [--json]`; without `--apply` it
  writes NOTHING and prints the plan, which is the default because a tool that
  migrates a machine on the strength of a typed word is one nobody trusts with
  a checkout.
  (6) **Plugin install is ATOMIC and uninstall is COMPLETE.**
  `install_from_local(source, name_override=None, *, source_kind="local")`
  now runs **stage** (copytree into `<plugins-root>/.staging-<name>-<pid>-<n>`,
  on the same filesystem as the destination so the swap is a rename) →
  **verify** (manifest re-read from the STAGED tree, `validate_plugin`
  re-run, symlinks re-rejected, tree SHA-256 + file/byte counts recorded) →
  **swap** (`os.replace` the old tree to `.trash-<name>-<pid>-<n>`, rename the
  staged tree in, then delete the trash; if the final rename fails the old
  tree is moved BACK and a failure to even restore names where it was
  preserved). Any failure before the swap leaves the previous version
  installed. `InstallReceipt` is written to
  `<plugins-root>/.state/<name>.json` (atomic unique-temp + `os.replace`).
  `uninstall(name) -> UninstallReport` (and `remove_with_report`) removes the
  tree, the install-state ROW, the `.disabled` marker, and any staging/trash
  residue, then RE-SCANS the whole plugins root: `remaining` is a measurement
  and `complete` derives from it. The report enumerates all four categories —
  files, database rows, logs, retracted path entries (the batch verbs and MCP
  labels the install record captured) — and states `os_path_modified: False`
  with the reason (a plugin install never edits the OS `PATH`, so reporting
  that it did would report something that did not happen). `remove(name)`
  keeps its exact historical signature and return value, delegates to
  `uninstall`, and now **RAISES** on a partial removal.
  (7) **`neo doctor` is a first-class operator surface and
  `neo support-bundle` produces one archive to attach to an issue.**
  `cli/doctor.py` gains `doctor_record()` (the same document, redacted end to
  end), `check_connector_permissions()` (a NEW registry row — an UNDECLARED
  connector is actionable with the exact remediation), and
  `build_support_bundle()` / `write_support_bundle()` /
  `render_support_bundle_manifest()`. A bundle is ten sections:
  `manifest.json` (per-file SHA-256 + byte count, `schema_version = 1`),
  `doctor.json` + `doctor.txt` (both renderers, markup stripped from the
  text one), `doctor-summary.json`, `environment.json` (interpreter, platform,
  Neo version, and the NAMES — never the values — of 19 Neo/provider-shaped
  environment variables), `config-shape.json` (per key: source tier, type,
  shape, and a value that is either masked through
  `cli.neoconfig.public_value` for the 11 keys on the public list or literally
  `withheld`), `connectors.json` (declared permissions, no launch commands),
  `hooks.json` (the merged hook config INCLUDING the fail-policy table in
  force, and the error text when a config is unparseable),
  `plugins.json` (installed plugins + install records), and
  `recent-errors.json` (the ten error-class journal events across the most
  recent run directories, bounded at 5 rows per event and 200 journals, every
  row truncated). Every value passes through `shared.security.redact_text`
  inside its own builder AND again on the manifest, so adding a section cannot
  make the bundle the one place a secret escapes. The zip is written through a
  unique temp name, a fixed 1980-01-01 entry timestamp (so two bundles of the
  same state are byte-comparable), and `os.replace`.
  `neo doctor` exits **0** with nothing actionable and **1** with an
  actionable row, because a doctor that always exits 0 cannot be a CI gate.
  (8) **NEW subcommands, all additive, no existing parser entry, help string,
  exit code, or dispatch path changed:** `neo doctor [--repo] [--log-root]
  [--json]`, `neo support-bundle [--repo] [--log-root] [--out] [--recent-runs]
  [--no-archive] [--json]`, `neo migrate` (above), `neo hooks list|run`,
  and `neo mcp permissions`. `mcp_server/`, `integrations/`, `execution/`,
  `runtime/`, `harness/`, `shared/`, and `memory/` were NOT edited.
  (9) **NEW `cli/migration.py` is the ONE new file this round created.** It is
  named here because a reviewer diffing the tree should not have to discover
  it; nothing else in the tree was added, and no other prompt's file was
  edited except one disclosed test pin (below).
  (10) **Verification actually run.** NEW
  `tests/test_r2_16_extension_ops.py` → **34 passed** (~44s), one test per
  required behaviour and named after it: the namespace layer reached by a
  REAL `python -m mcp_server` stdio subprocess; an undeclared tool refused on
  that same real server; a pin whose digest moved refused; a hook that raises
  handled per its documented class (fail-closed refuses, fail-open allows and
  still records); a real missing-executable (127) handled the same way; the
  fail-policy table total with a reason per event; a per-tier and a per-hook
  override recorded as such; a broken `Stop` hook unable to leave a verified
  status intact; a network-permissioned connector gated (allowed host vs
  undeclared host vs no-host-at-all); `write = false` refusing a mutating tool
  while an undeclared `write` does not; a malformed declaration raising; an
  undeclared connector reporting `enforced: false`; `neo migrate` idempotent
  (second run `already_current`, zero actions, zero outcomes), its plan
  read-only and naming exact paths, its rollback restoring an earlier write
  AND a removal, its destructive step gated on the flag, and its receipt
  backfilling a pre-atomic install; plugin install interrupted at THREE points
  leaving the previous version byte-identical with no residue; a measured
  install record; plugin uninstall leaving nothing behind (asserted by walking
  the root); a partial removal RAISING; `doctor --json` and the support bundle
  proven secret-free against a planted `sk-…` in settings AND in the
  environment, with the archive BYTES asserted; the directory form matching
  the archive; recent errors collected with their bounds; `neo doctor`'s exit
  code; the new doctor row; every new subcommand registered and dispatchable;
  and a real `python -m cli --help` subprocess.
  `tests/test_cli_connectors.py` → 27 passed. `tests/test_cli_plugins.py` +
  `tests/test_extensions.py` → 67 passed, 2 platform skips (the Windows
  symlink-privilege cases; not passes). `tests/test_ceiling12_hooks.py` → 36
  passed, 1 platform skip. `tests/test_cli_power_tools.py` +
  `tests/test_cli_config.py` + `tests/test_cli_neo3.py` → 172 passed.
  `tests/test_mcp_server.py tests/test_mcp_adversarial.py
  tests/test_memory_mcp_release.py tests/test_integrations.py` → 76 passed, 2
  platform skips. `tests/test_cli.py tests/test_cli_release.py
  tests/test_cli_neo.py tests/test_cli_neo2.py` → 104 passed. `python -m
  evals.run --check` → **14/14 CLEAN**. `python -m ruff check` clean on every
  file this round created or edited; `ruff format` applied to the two new
  files; `python -m compileall -q` clean on all six.
  **No Docker lane and no live-provider lane was run and neither is claimed.**
  (11) **The ONE edit outside this prompt's files, disclosed:**
  `tests/test_cli_power_tools.py::TestDoctor::test_json_document_shape_is_stable`
  had its doctor-count pin moved 9 → 11 and its key set extended with
  `connector_permissions` (this round) and `spend_receipts` (a parallel
  terminal's row). A registry entry necessarily changes the count, and the
  pin's stated intent — "a silently dropped or duplicated check is a defect" —
  is preserved by keeping the count exact rather than by removing it. No
  assertion was weakened.
  (12) **Cross-terminal requests, not applied here** (full text in
  `cli/AGENTS.md`): `harness/agent_kernel/kernel.py` (and
  `harness/agent_loop.py`) should dispatch `HookEngine` at the authoritative
  tool/completion boundaries so the kernel's own tool calls are gated by the
  same `PreToolUse`/`Stop` policy, and should route external MCP calls through
  the same gate; and `memory/mcp_client.py::list_mcp_tools` should preserve
  each tool's `inputSchema` and declared side-effect class instead of
  normalizing to `{name, description}`, because a side-effect ceiling and a
  network-host gate cannot be evaluated from a name alone.
  **Checked and NOT requested:** `pyproject.toml` already lists `extensions` in
  `[tool.setuptools] packages`, so the standing "add `extensions` to packaging
  discovery" note in `extensions/AGENTS.md` was stale and has been corrected
  rather than filed as work.
- 2026-09-27 (R2-14 - budget governor, quota awareness, 429 safety): **NEW
  `runtime/budget_governor.py`; `runtime/provider_resilience.py`,
  `runtime/provider_gateway.py`, `runtime/worker.py`, `runtime/scheduler.py`,
  `runtime/config.py`, `runtime/fake_harness.py` and the budget-check region of
  `harness/core.py` changed additively. `runtime/model_router.py` was NOT
  edited (R2-13 owns it) and NO Boundary 0-5 signature, event kind, serialized
  field, completion status, or verifier mint changed.** The root cause was that
  nothing above the provider dial knew about money, quota, or its own clock;
  `runtime/budget_governor.py` is now the single authority for all three and
  everything else asks it.
  (1) **THE BACKOFF AND THE WATCHDOG NOW AGREE, ON ONE MECHANISM.** The
  approval-park marker is GENERALIZED, not duplicated: the runtime checkpoint
  gains `supervision_exemption = {reason, until_epoch, seconds, grace_s, kind,
  clock}` alongside the unchanged `awaiting_approval` boolean, and
  `runtime.budget_governor.state_stale_exempt(checkpoint) -> (exempt, reason)`
  is the single reader. `scheduler._check_timeouts` consults it for the
  STATE-STALE kill only; the heartbeat kill and the wall-clock cap are never
  exempted, an EXPIRED exemption is not an exemption, and `status="finished"`
  remains exempt. The window is DERIVED from `backoff_seconds(attempt, ...)` -
  the same function the retry loop sleeps - so a backoff longer than the hang
  window can no longer be read as a hang. The WAIT (`seconds`) and the LICENCE
  (`seconds + grace_s`) are deliberately separate numbers, and `grace_s` IS the
  watchdog's own staleness window: `runtime/config.py::DEFAULT_HANG_STALE_S`
  (re-exported as `scheduler.DEFAULT_HANG_STALE_S`, same object) is read by both
  the scheduler's kill threshold and the worker's grace. Every skip is journaled
  as `hang_exempt` with the reason and the measured `state_age_s`; the
  suppression is journaled as `hang_exempt_suppressed`. `Task.config
  ["hang_backoff_exempt"] = False` (absent = on) restores the pre-round
  state-stale kill for a backing-off worker and is the measured OFF arm; the
  approval-gate exemption is deliberately NOT switchable.
  (2) **QUOTA IS NO LONGER A RATE LIMIT.** In
  `runtime/provider_resilience.py`, `"quota exceeded"` and
  `"insufficient_quota"` used to be members of `_RATE_LIMIT_MARKERS`, so an
  exhausted account was served backoff-and-retry. They now live in their own
  `QUOTA_MARKERS` set (re-exported from the governor, so there is one table)
  and `classify_retry` tests them FIRST, returning `kind="quota_exhausted"`,
  `retryable=False`, `replayable=False`, `provider_fault=False`. It is not a
  provider fault on purpose: the endpoint is healthy and the account is empty,
  so no circuit breaker is tripped and no failover is attempted. New public
  `runtime.budget_governor` vocabulary: `QUOTA_EXHAUSTED`, `RATE_LIMITED`,
  `PROVIDER_UNAVAILABLE`, `AUTH_FAILED`, `BAD_REQUEST`, `UNKNOWN_FAILURE`,
  `TERMINAL_KINDS`, `QUOTA_MARKERS`, `QUOTA_BILLING_URLS`, `billing_url_for`,
  `ProviderFailure`, `classify_provider_failure`, `QuotaExhausted` (carrying the
  failure and a non-empty `billing_url`). In
  `provider_gateway.resilient_call_model` a `QuotaExhausted` re-raises
  immediately: no retry, no backoff, no next candidate. Status code is
  deliberately not the discriminator, because several providers report quota as
  a 429.
  (3) **`TaskResult.status` IS UNCHANGED, AND THAT IS THE POINT.** The
  terminal verdict is machine-readable in FIVE places instead of a new
  `Literal` member in another owner's file: the runtime checkpoint's
  `terminal_reason` + `quota`, the scheduler run journal's
  `finish.terminal_reason`, `logs/{task_id}/budget.json`'s `quota` block, the
  `quota_exhausted` unified-trace event, and the exception text. `run_task`'s
  status stays inside the existing four-value vocabulary (a quota wall mints NO
  verification and NO diff). Adding `"quota_exhausted"` to
  `shared.types.TaskResult.status` is filed as a cross-terminal request in
  `runtime/AGENTS.md` §6, not applied.
  (4) **PER-CALL BUDGET, WITH THE GRANULARITY STATED.** New
  `BudgetGovernor.authorize_call/commit/release/spent_usd/remaining_usd/
  reserved_usd/exhausted/note_quota/begin_backoff/sleep_in_backoff/report`, new
  `BudgetVerdict` (`cap_usd`, `spent_usd`, `reserved_usd`, `remaining_usd`,
  `price_state`, `model`, `reason`), `BudgetRefused`, `estimated_call_price`,
  `install_governor`/`current_governor`/`clear_governor`,
  `write_budget_receipt`, and `MAX_BACKOFF_S`. The pre-check is installed at BOTH
  dial boundaries: `provider_gateway` (before a request is built; a refusal
  raises `BudgetRefused` and is recorded on the ledger as
  `skipped_reason="budget_refused"`) and `harness.core._bind_budget_guard`
  (an idempotent wrap of the run's `ModelClient.call`). **The enforced bound is
  `cap + the price of the final call`, not `cap`** - a call's price is not
  knowable before it is made, so the reservation is an estimate; declaring
  `max_completion_tokens` makes it a sound upper bound. This replaces the
  measured 9.28x attempt-level bound with +1 call. `harness.core.over_budget()`
  reads `governor.exhausted` when a governor is bound and otherwise evaluates
  the historical expression byte-identically, emitting
  `budget_governor_absent` so attempt-level-only enforcement is never
  indistinguishable from a cap that was never configured. A refusal is STICKY,
  and `spent_usd()` is the MAXIMUM (never the sum) of the governor's charges
  and the bound `spend_source`, so the cap can only fire EARLIER than either
  source alone. **The price and its state come from R2-13's
  `model_capabilities.estimate_cost(...)`; the governor has no price table of
  its own and never infers price state from a number.** `BudgetVerdict
  .price_state` is R2-13's `priced`/`free`/`unpriced` plus `declared_bound`,
  `uncapped`, `cap_reached`, `quota_source`; an unpriced model with no declared
  bound is REPORTED, not rounded to zero.
  (5) **A LIVE RECEIPT.** `logs/{task_id}/budget.json` (next to `state.json` and
  `trace.jsonl`), written through the one atomic writer and published at
  construction, carries the cap, the spend, the reservations, the REMAINING
  budget, `exhausted`, `deadline_epoch`, the clock name, the backoff block
  (base/cap/grace/count/seconds_total/live window), the quota block, call
  counters, and a bounded verdict log. Each publish also emits a `budget`
  unified-trace event. An unwritable receipt is reported, never raised.
  (6) **`governed_completion` is the ONE dial loop.** It replaces
  `model_router._completion_with_retry` on the resilient path with three
  distinct actions - quota raises on the FIRST attempt (zero retries, zero
  sleeps), rate limit arms the supervision exemption for exactly the wait it is
  about to sleep and then sleeps it, transient keeps its bounded short retry -
  and a non-idempotent call is dialled exactly once. `KeyboardInterrupt` and
  `SystemExit` are re-raised before classification.
  (7) **KEYS AND DEFAULTS.** No R2-14 key is in any `DEFAULTS` table (a value
  merged into every task switches every task and every eval arm), and a test
  fails if one appears. `budget_cap_usd` stays a pre-existing
  `harness/config.py` DEFAULTS key; the worker resolves it through
  `harness.config.get_config` so a caller relying on the default still gets a
  cap. New optional keys: `budget_reserve_per_call_usd`,
  `hang_backoff_exempt`. `DEFAULT_HANG_STALE_S` MOVED from `scheduler.py` to
  `runtime/config.py` and is re-exported from `scheduler.py` as the SAME
  object, so the kill threshold and the grace are one constant.
  (8) **VERIFICATION.** NEW `tests/test_ceiling_r2_14_budget_quota.py` -> **60
  passed** (host-only; no Docker, no provider, no network), including a real
  Scheduler + real worker subprocess proving a 4.0 s 429 backoff survives a
  2.5 s hang window and its A/B OFF arm, a real worker ending as
  `quota_exhausted` with `backoff.count == 0` and no crash-retry, and a
  self-calibrating cap whose ledger sum is asserted to be `<= cap + the final
  call's price`. `test_scheduler_integration test_model_router
  test_difficulty_approval test_ensemble test_ceiling14_resilience` -> **168
  passed**; `test_config_trace_state test_stubs_and_deps test_modes` -> **123
  passed**; `test_recovery_steering test_tracing test_evals_run
  test_evals_tasks` -> **141 passed**; `test_orchestration test_agent_kernel
  test_agent_loop` -> **128 passed**; `test_e2e_run_task` -> **28 passed in
  906.85s** (REAL Docker sandbox + verifier);
  `test_ceiling_r2_13_capability_routing` -> **60 passed** (run after the
  governor was rewired onto R2-13's `estimate_cost`); `python -m evals.run
  --check` -> **14/14 CLEAN**; `python -m evals.run --quick` -> **40/40, verdict
  CLEAN, 0 regressions** through the real loop and the real Docker verifier.
  **NOT run / not claimed:** no live-provider lane, no full Docker prompt
  matrix, and no `runtime.stress` / `runtime.soak` / `runtime.abuse` run - so
  the 9.28x figure recorded in `runtime/AGENTS.md` and `runtime/abuse.py` is the
  PRE-round number and now OVERSTATES the bound; re-running the `overshoot`
  scenario is the natural regression proof.
  (9) **CROSS-TERMINAL REQUESTS, not applied:** add
  `"budget_governor"` to `model_router._RESILIENCE_CONFIG_KEYS` (R2-13's file) -
  without it a task carrying a governor but no other Ceiling-14 key never
  enters the pipeline, so it gets the harness-loop pre-check but not the
  dial-boundary one; `TaskResult.status` needs `"quota_exhausted"` if the
  status is to be switchable; `cli/runview.py` / `cli/tui.py` should render
  `budget.json` and the quota `billing_url`; and `harness/model_client.py` is
  the better home for the pre-check if the kernel's `ModelGateway` path
  should be covered too (it is not today). All four are written out in
  `runtime/AGENTS.md` §6.
- 2026-09-27 (R2-09 - the substrate is scaled: a hoisted symlink check,
  stat-based freshness, sparse PageRank, and a budgeted retrieval path that
  labels its own truncation): **NEW additive surfaces in
  `memory/code_graph.py`, `harness/retrieval.py` and `harness/knowledge.py`.
  No Boundary 0-5 signature, return shape, serialized field, event kind or
  completion status was changed, removed, renamed or weakened. In particular
  `retrieve_context` still returns EXACTLY `{terms, files, greps, strategy}`
  (`tests/test_retrieval_tools.py::test_retrieve_context_shape` is unmodified
  and green), `rank_symbols` still returns a list, `CodeGraph(repo, root)`
  keeps its positional signature, and no verifier, gate or completion status
  was touched. `harness/core.py`, `harness/agent_loop.py`, `harness/config.py`
  and `harness/agent_kernel/**` were NOT edited by this round.**
  (1) **THE MEASUREMENTS THIS IS BASED ON** (this repository, 289,584 files,
  before the change): `_has_symlink_component` **4.97 ms/file** (23.9 min
  projected at 288k files); the source-digest pass **did not finish inside a
  50-minute budget**; a full graph build **1,987.5 s**; full-graph PageRank
  **69.16 s at 1,600 vertices** (cleanly quadratic: 3.90 / 17.75 / 69.16 /
  298.17 s at 400 / 800 / 1,600 / 3,200); end-to-end retrieval **did not
  finish inside a 40-minute budget**. Platform costs that make the shape of
  the fix obvious on this host: `os.lstat` **by path 105 us** against the same
  fact off an `os.scandir` entry **0.18 us**, and a `WindowsPath` object
  **17.75 us**. After: hoisted check **5.31 us/file** with the required
  300,000-file synthetic tree in **1.592 s**; digest pass **17.50 s cold /
  0.222 s warm**; build **28.4 s**; PageRank **0.0417 s at 1,600** (1,659x,
  now linear); budgeted retrieval **7.3 s at a 5 s budget** and **30.3 s at a
  30 s budget**.
  (2) **`memory.code_graph.PathSafety`** classifies each DIRECTORY once, from
  the `os.scandir` entry the walk already read, and answers "is any component
  of this path a symlink?" with a set lookup and no syscall
  (`for_root`, `key_of`, `observe`/`observe_key`/`observe_dir`/
  `observe_file`, `has_symlink_component`/`has_symlink_component_key`,
  `is_safe`, `root`, `stats`). `_has_symlink_component` is KEPT unchanged as
  the per-path fallback. The shared walk is `iter_source_entries(...)` ->
  `SourceEntry` (with a lazily built `path`), used by the builder, by
  `_snapshot_mtimes`, and by `snapshot_sources`, so `load_or_build` +
  `_save_locked` no longer walk the repository four times. An unreadable
  component is still UNSAFE (fail closed), and a symlinked file is still
  refused and a symlinked directory still never entered (test-pinned).
  (3) **Stat-based freshness, with the blind spot named.**
  `snapshot_sources(...) -> SourceSnapshot` (`.digests`, `.stats`,
  `.digest_source`, `.content_digested`, `.stat_reused`, `.blind_spot`,
  `.to_dict()`, `.receipt()`); constants `DIGEST_SOURCE_CONTENT|STAT|MIXED`
  and `DIGEST_SOURCE_VALUES`. A file whose `(size, mtime_ns)` is unchanged
  keeps its previous digest WITHOUT being read. `digest_source` says which
  path ran, and `SourceSnapshot.blind_spot` / `receipt()["stat_blind_spot"]`
  name the miss class verbatim: *a content-only edit that preserves both size
  and mtime_ns is not detected on the stat-reused path*. That is an
  optimization with a documented blind spot, NOT a correctness claim, and it
  is closable: `CodeGraph(repo, root, *, verify_digests=None)` is keyword-only
  and TRI-STATE, and `verify_digests=True` forces the content pass and restores
  the historical same-size/preserved-mtime guarantee. `meta.json` gains the
  additive `source_stats`, `digest_source` and `digest_receipt` keys and
  keeps `mtimes` / `source_digests` byte-identical in shape; `CodeGraph` gains
  a `freshness_receipt` property. The config hook is
  **`code_graph_verify_digests`, by KEY PRESENCE and deliberately NOT in
  `harness/config.py` `DEFAULTS`** (a default is merged into every task and
  every eval arm, so one would switch all of them at once).
  (4) **`harness.retrieval.sparse_pagerank(node_ids, edges, seeds, *,
  frontier=0, iterations=30, damping=0.85, deadline_s=None) -> (ranks,
  receipt)`** is the new public ranking primitive, and `_pagerank` is now
  O(V+E) per iteration instead of O(V^2) (the per-edge arithmetic is
  unchanged, so the ranks are the same numbers). `rank_symbols_with_receipt(
  ...) -> (rows, receipt)` is the truncation-aware ranking; `rank_symbols`
  delegates to it and gains keyword-only `frontier` / `budget_s` /
  `receipt`. `pagerank_symbols` / `weighted_symbol_ranking` /
  `repository_map` are unchanged. The receipt is ALWAYS populated and says
  which path ran: `truncated`, `restricted`, `mode` (`full` | `sparse`),
  `frontier`, `vertices_total`, `vertices_ranked`, `seeds`, `not_searched`,
  `not_searched_files`, `digest_source` (+ `stat_blind_spot`) and `elapsed_s`.
  A requested top-k is NOT reported as truncation; only a bound (frontier,
  deadline) is. Because the default path is now seed-and-restrict, a
  `rank_symbols` call on a graph larger than the seed's reachable set reports
  `truncated: true, truncation: "sparse"` - that is the honest description of
  an answer that stopped running a whole-graph iteration.
  `load_code_graph` gains keyword-only `receipt=None` carrying the index's own
  digest provenance.
  (5) **`RetrievalOutcome(result, receipt)`** with `.truncated` / `.to_dict()`,
  `retrieve_context_budgeted(...) -> RetrievalOutcome` (the real
  implementation; `retrieve_context` is now a thin wrapper over it and gained
  ONE additive keyword-only `budget_s`), `walk_code_files_budgeted(...) ->
  (files, not_searched)`, and the labels
  `TRUNCATION_COMPLETE|BUDGET|FRONTIER|SPARSE|ERROR` (`complete` is the only
  value that may be presented as a whole answer). A wall-clock budget bounds
  the stages AND the two whole-repository scans; on exhaustion the call
  returns the best-ranked results computed so far, keeps ``truncated: true``,
  and names what was not searched. **A truncated ranking is never cached**, so
  a later call cannot serve a partial answer as a complete one. The
  `retrieval_context` trace payload gains additive `truncated`,
  `truncation`, `not_searched`, `not_searched_files`, `budget_s`, `elapsed_s`,
  `stages` and `error` keys. The single stage that is NOT interruptible is a
  cold index BUILD (`CodeGraph.load_or_build`), because a half-built index is
  a wrong index; its measured cost is reported in
  `receipt["stages"]["structural"]` rather than hidden.
  (6) **`harness.knowledge`**: `KnowledgeContext.retrieval_budget_s` (read by
  KEY MEANING - absent means "no budget", explicit `0` is a measurable OFF
  arm, an unusable value degrades to "no budget" plus a warning),
  `.retrieval(...) -> RetrievalOutcome` (never raises; emits `retrieval` /
  `retrieval_truncated`; counts `stats["retrievals"]` /
  `stats["retrievals_truncated"]`), `.retrieval_receipt`, and
  `render_truncation_note(outcome) -> str` (the model-facing line; `''` for a
  complete result; never raises). The budget key **`retrieval_budget_s` is
  deliberately NOT in `harness/config.py` `DEFAULTS`**, for the same reason as
  above. The receipt is in `as_dict()` and `close()` reports
  `retrieval_truncated`.
  (7) **Two defects found BY MEASURING, both in code this round owns.** (a)
  `harness.retrieval._structural_scores` built the whole code graph into a
  fresh `tempfile` directory on every retrieval call when `index_root` was
  None - re-parsing the entire repository per call (measured 33-45 s) and
  discarding the index. The comment justifying it ("CodeGraph's own default
  root lives INSIDE the repo") has been STALE since Ceiling-03: the default is
  `harness_home()/code-graph`, outside the repository. It now resolves
  through `load_code_graph`, so the persistent content-digest cache is
  actually used; the never-mutate guarantee is unchanged and
  `test_structural_never_writes_into_original_repo` still passes. (b)
  `_caller_id` in `memory/code_graph.py` scanned EVERY node of the graph, for
  each of the three node-kind prefixes, for every call site - O(call sites x
  nodes), which was **1,970 s of the 1,987.5 s build**. It is now indexed by
  `_qualified_index(graph)` (built once per build, in `graph.nodes` insertion
  order so the candidate list and its order are identical), with
  `_caller_id_reference` retained as the linear-scan TEST ORACLE.
  (8) **Public surface added:** `PathSafety`, `iter_source_entries`,
  `SourceEntry`, `SourceSnapshot`, `snapshot_sources`, `_qualified_index`,
  `_caller_id_reference`, `DIGEST_SOURCE_*`; `sparse_pagerank`,
  `rank_symbols_with_receipt`, `RetrievalOutcome`,
  `retrieve_context_budgeted`, `walk_code_files_budgeted`,
  `TRUNCATION_*`; `KnowledgeContext.retrieve`,
  `KnowledgeContext.retrieval_budget_s`,
  `KnowledgeContext.retrieval_receipt`, `render_truncation_note`.
  **Changed behaviour a consumer may notice:** (i) `rank_symbols` /
  `rank_repository_map` no longer rank the whole graph by default, so their
  receipts say `truncated: true, truncation: "sparse"` whenever the seed set
  is smaller than the graph; (ii) the code graph's freshness check skips
  reading unchanged files unless `verify_digests=True`; (iii) a truncated
  retrieval is not cached.
  (9) **Verification (real, this tree, `-p no:randomly`):** NEW
  `tests/test_ceiling_r2_09_scale.py` **16 passed, 1 skipped** (Windows
  symlink-privilege). `tests/test_retrieval_tools.py` 27 passed, 1 skipped
  (its exact-four-key assertion unmodified and green).
  `test_code_graph test_multilang_graph test_decision_store test_mcp_server
  test_mcp_client test_mcp_stdio_fileno test_mcp_adversarial
  test_retrieval_tools test_ceiling05_knowledge test_prompt_cache_cost` ->
  **224 passed, 3 skipped**. The scale + retrieval + graph + knowledge +
  cache-cost selection -> **152 passed, 3 skipped**. `python -m evals.run
  --check` -> **14/14 CLEAN**. `ruff check` clean on every touched file.
  `compileall` clean. **The 300k-file proof was RUN** under
  `NEO_R209_SCALE=1` and measured 1.592 s.
  (10) **Not implemented, stated plainly.** No embeddings or semantic
  retrieval (the ranking is still lexical + structural, and call resolution is
  still Terminal 4's documented name-based over-approximation). A cold index
  build is not interruptible, because a partial index is a wrong index. The
  `PathSafety` index is not thread-safe (one per instance, as each owns its
  lock). A caller that renders only `result` and ignores the receipt still
  renders a partial answer; `render_truncation_note` exists for that and no
  production call site was changed to use it, because those files belong to
  other terminals (filed in `harness/AGENTS.md` §8 and `memory/AGENTS.md` §6).
  `_SKIP_DIRS` in `harness/retrieval.py` is the single largest remaining
  retrieval cost and widening it is a retrieval-QUALITY decision, so it is
  filed as a request with its measurement rather than taken: the same walk
  opens 155 directories in 0.42 s with the code-graph skip set and 124,774 in
  144.6 s with retrieval's. Measurements on this repository carry rebuild
  noise because parallel terminals were editing it throughout; the ratios and
  per-file costs are stable, the absolute end-to-end numbers are not. **No
  Docker lane and no live-provider lane were run and neither is claimed.**
- 2026-09-27 (R2-13 - a model capability registry, an `unpriced`-is-not-`$0`
  cost report, a capability-constrained router, and a structural difficulty
  predictor that was measured and NOT shipped): **`runtime/model_capabilities.py`
  becomes the ONE capability authority (context window + tool-calling +
  reasoning-content + streaming + PRICE); `runtime/model_router.py` gains an
  opt-in gate and the R2-14 `reasoning_content` protocol; `runtime/difficulty.py`
  gains a structural feature family and a held-out comparator; NEW
  `evals/routing_capability.py` and `evals/difficulty_holdout.py`. No Boundary
  0-5 signature, event kind name, serialized field, completion status, or
  verifier mint changed, and no existing `_PRICES` / `_fallback_cost` /
  `call_model` behaviour changed for an unconfigured caller.**
  (1) **The price table moved; it did not fork.** `MODEL_PRICES` in
  `model_capabilities` is the authority and `model_router._PRICES` is a
  re-export of that exact object, so `runtime/ablation.py` and every existing
  cost reader keep working against ONE table. A key's PRESENCE is what makes a
  model priced: absence is `unpriced` and must never be compared as `0.0`,
  because the price ladder picks the cheapest tier by comparing numbers and an
  absent row read as zero makes an unpriced model the CHEAPEST one.
  `PRICE_STATES == ("priced", "free", "unpriced")` is closed; `free` is
  reachable only by a DECLARED `(0.0, 0.0)` row. Every ledger row and every
  `get_last_usage()` record gains `price_state` + `cost_priced` beside
  `cost_usd`, and `cost_source` for an unpriced model is now `"unpriced"`
  rather than the old `"unknown"` (a state, not an admission that a number was
  produced). `estimate_cost` consults the registry as well as the table, so an
  operator-declared rate is not invisible to the cost report.
  (2) **The registry is tri-state, and that is the point.** `ModelCapability`
  carries `context_window`, `supports_tools`, `supports_reasoning`,
  `supports_streaming` as `True | False | None`, where `None` means UNKNOWN and
  is never read as `False` — a registry that reported "cannot call tools" for a
  model nobody has described would refuse to route the entire world.
  `register_capability` / `unregister_capability` / `lookup_capability` /
  `capability_of` / `known_capabilities` / `unknown_capability` /
  `reset_capability_registry` / `price_of` / `declared_rates` / `estimate_cost`
  are the public surface. A malformed declaration RAISES `CapabilityError`
  (a negative rate, an unparseable flag, a non-positive window); an ABSENT rate
  does not. Lookup order: exact `(provider, model)`, then `("", model)`, then
  ANY registered row for that model name (the provider-agnostic fallback the
  cost report needs, since it is handed a model and no provider), then the
  built-in row; then `None`, never a fabricated row.
  (3) **The gate is opt-in by KEY PRESENCE and can REFUSE.** `_CAPABILITY_CONFIG_KEYS`
  is `("capability_gate", "capability_allow_unpriced", "capability_strict_tools",
  "capability_tool_driven", "model_capabilities")` and the test is
  `any(key in ctx)` — exactly the Ceiling-14 resilience pattern, because
  `capability_gate: False` must still opt IN (the operator wants the pipeline
  and turned the feature off inside it). **NONE of these keys is in
  `harness/config.py::DEFAULTS`, not even as `None`**, because a `DEFAULTS`
  entry is merged into every task config and every eval arm and the switch is
  presence: an entry would switch the gate on everywhere and start refusing
  unpriced targets in runs that never asked. `harness/config.py` carries a
  comment block saying exactly that, and
  `test_the_shipped_default_configuration_cannot_switch_the_gate_on` pins it.
  `screen_candidates` refuses a router-chosen unpriced target, a DECLARED
  tool-incapable model on a tool-driven call, and (under
  `capability_strict_tools`) a merely UNKNOWN tool capability; the first
  eligible candidate wins and the rest of the tier table, in ASCENDING
  difficulty order, are the fallbacks, so an exclusion ESCALATES rather than
  downgrades. With nothing eligible it raises `CapabilityRoutingRefused` with a
  reason from `REFUSAL_REASONS` and every candidate it considered.
  (4) **The unpriced refusal has exactly one documented exemption:** a model
  the CALLER named explicitly (call-level `model`, context `model`, or
  `provider_profile.model`). The prompt is to refuse to *route* on an unpriced
  model, and a caller who pinned the model is not routing. The exemption is
  recorded as `explicit_unpriced_bypass: true` and never silent, the cost
  report still says `unpriced`, and the TOOL constraint still applies to an
  explicit model — a loop that needs tool calls cannot use a model that cannot
  make them. `capability_allow_unpriced` is the same opt-in spelled as config.
  (5) **Tool-driven is decided from the call, not guessed.** `tools` being
  non-empty makes the call tool-driven; `capability_tool_driven` declares a
  loop-level fact for a call that carries no schemas of its own (a planner call
  inside a tool-driven run). A plain completion is NOT gated on tool support —
  it has no tools to emit, and excluding it would be an invented constraint.
  (6) **R2-14 `reasoning_content` protocol.** `_extract_reasoning` reports
  THAT reasoning was present and how much of it there was, from the message
  field or `usage.completion_tokens_details.reasoning_tokens`, and **never
  returns it as the answer.** An empty visible answer is now THREE outcomes,
  not one: `truncated_reasoning` (reasoning present AND
  `finish_reason == "length"` — the completion budget went to thinking, which
  is a budget problem), `empty_response_reasoning_only` (reasoning present,
  not budget-limited), and `empty_response` (nothing at all). All three raise,
  as the historical code did, so no completion semantics changed — but the
  ledger row now carries `reasoning_content_present` / `reasoning_chars` /
  `reasoning_tokens` and a `stop_reason` that names the real cause, instead of
  one message that sent the operator after the wrong knob. Reasoning is never
  substituted for content, so a truncated turn cannot be laundered into a
  successful-looking one. `supports_reasoning` in the registry is the
  capability that documents this shape per model.
  (7) **A structural difficulty predictor, built, measured, and NOT enabled.**
  `runtime/difficulty.py` gains `repo_shape`, `symbol_fan_in` (stdlib-ast
  import graph + definition reachability), `target_test_exists`,
  `classify_bug_class` (closed vocabulary, declared weights), `structural_features`,
  `score_to_structural_hint`, `predict_structural`, `group_holdout_split` and
  `compare_predictors`. It is reachable ONLY by
  `Task.config["difficulty_features"] = "structural"`; `"auto"` resolves to the
  incumbent because `model_router._structural_calibrated()` reads a sibling
  artifact that nothing writes unless a held-out win justifies it. The
  incumbent heuristic, its bands, its calibration-file override, and
  `predict_difficulty`'s signature and return shape are all UNCHANGED, and the
  eval matrix's ablation path through it still runs.
  (8) **The honest negative, measured, on THIS repository's real run history**
  (`python -m evals.difficulty_holdout --logs-root logs --apply`, 107 real
  adaptively-routed fix runs, 65 bug groups, strict label policy, grouped
  split): **the structural predictor WON the held-out split — accuracy 0.9412
  vs 0.8235, false escalations 0 vs 2, missed escalations 1 vs 1 — and is
  still NOT shipped**, because that split carries **1** hard-labelled
  observation against `MIN_HOLDOUT_HARD_LABELS = 3`. A win on one hard case
  can be one lucky prediction, and the report says
  `promising-but-unproven` rather than "apply". `--apply` wrote nothing;
  `runtime/difficulty_structural_calibration.json` does not exist.
  `compare_predictors` reports `winner`, `decidable` (the holdout has a hard
  label at all), `sample_adequate` (it has at least the declared floor), and
  `ship` (winner AND adequate) separately, so a win and a shipping decision
  can never be read as the same claim.
  (9) **Two of the five features the round named are not measurable in this
  history, and the report says so per feature.** `repo_size` 107/107,
  `test_density` 107/107, `bug_class` 107/107, but `target_test_missing`
  18/107 (runs that rely on test autodetection declare none) and **`fan_in`
  0/107** — run history records touched FILES and carries no per-task
  touched-SYMBOL list, so the fan-in feature is UNMEASURED rather than
  measured-as-zero. `feature_measured` reports each one and
  `feature_notes` names the reason. Producing symbols is a cross-terminal
  request in `runtime/AGENTS.md`.
  (10) **The ablation covers capability selection, not just price.**
  `evals/routing_capability.py` runs `price_only` (no capability key — the
  pre-R2-13 behaviour), `capability`, and `capability_strict_tools` over a
  fixed five-case set through the REAL router (offline mock provider, zero
  network, no credential read; only the model RESPONSES are mocked, the target
  resolution, the capability screen, the price ladder and the cost report are
  the shipping code). Measured: **21 checks, 0 regressions, verdict CLEAN.**
  The control arm demonstrably still selects the cheapest tool-INcapable model
  and reports it, which is what makes the gated arm's different answer
  attributable to the gate rather than to a config difference. The fictional
  `acme/*` models are registered through the real registry. It was deliberately
  NOT added to `evals/run.py::ARMS`: that suite pins `adaptive_routing: False`
  and `use_mock_provider: True`, so a capability arm there would compare two
  identical configurations and report a vacuous "no delta".
  (11) **One real defect this round found and fixed in the price path, plus two
  in its own features.** The cost path read only the global table, so a rate
  registered for a model the table does not know was reported `unpriced` — the
  "we do not know" answer about a model whose price the operator had stated.
  `declared_rates` fixes it and is pinned both ways. `symbol_fan_in`
  under-counted every symbol by exactly the from-import half of a codebase,
  because `from M import x` registered only `M.x` and not `M`. The
  empty-response path appended TWO ledger rows for one failure (the specific
  receipt, then the generic handler's thinner one); the specific receipt now
  marks itself recorded. A fourth defect was in the TEST, not the code, and is
  the reason `test_the_exclusion_is_attributable_to_the_gate_not_to_a_missing_hint`
  exists: a capability test that omitted `adaptive_routing` read the built-in
  medium tier's identity as a capability decision and would have passed
  vacuously.
  (12) **Verification.** `tests/test_ceiling_r2_13_capability_routing.py` ->
  **60 passed** (host-only: no Docker, no network, no credential).
  `tests/test_model_router.py tests/test_difficulty_approval.py
  tests/test_prompt_cache_cost.py tests/test_evals_run.py tests/test_evals_tasks.py
  tests/test_config_trace_state.py` -> **128 passed**.
  `tests/test_ceiling14_resilience.py tests/test_ensemble.py
  tests/test_analyze_history.py tests/test_scheduler_integration.py` ->
  **172 passed**. `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
  `python -m evals.routing_capability` -> **CLEAN, 21 checks, 0 regressions**.
  `python -m evals.difficulty_holdout --apply` -> **NOT shipped** (above).
  `ruff check` clean on every file this round owns or created.
  `runtime/model_router.py` still carries its two PRE-EXISTING `F401`s
  (`typing.List` / `typing.Tuple` unused, from the Ceiling-10 streaming edit);
  they were deliberately left alone rather than "fixed" inside a shared file.
  **NOT run: no live-provider lane and no Docker lane**, and neither is
  claimed. The capability measurements are about WHICH MODEL GETS SELECTED and
  what the cost report says; they are not model-quality, latency, or token
  evidence, and only a live provider can produce that.

- 2026-09-27 (R2-15 - the daily path is sandboxed, and a grant is not a bypass):
  **`shared/approval.py` gains an additive trust section; `harness/agent_kernel/policy.py`
  and `cli/interactive.py` change; `harness/config.py` gains keys and ONE
  behaviour-changing default. No Boundary 0-5 signature, event kind, serialized
  field, completion status, or verifier mint changed, and `shared/security.py`,
  `shared/types.py`, and `cli/tui.py` were NOT edited.**
  (1) **The daily interactive path is SANDBOXED BY DEFAULT.** This is a
  behaviour-changing default and it is the fix: the daily path defaulted to
  `approval=auto` on LIVE HOST BASH with no sandbox while `neo fix` ran sandboxed
  behind a verifier gate, so the path a user trusts LEAST had the weakest
  boundary. `DEFAULTS["agent_process_sandboxed"]` is now `True`; it is read in
  exactly ONE place (`harness/agent_kernel/strategy.py`'s `shell` handler, passed
  to `SafeToolBackend.execute(..., sandboxed=...)`). MEASURED before the flip:
  `test_agent_kernel` + `test_ceiling_r2_04_daily_default` + `test_tool_protocol`
  + `test_workspace_security` 148 passed before AND after; `test_cli_terminal_parity`
  + `test_cli_command_system` + `test_ceiling14_resilience` 171 passed after;
  `evals.run --check` 14/14 CLEAN after. Real-Docker end-to-end through the same
  backend the daily strategy builds: default `profile=docker` 3.0s vs opt-out
  `profile=local_trusted` 0.3s.
  (2) **The opt-out is a config key with a loud, permanent receipt.**
  `daily_sandbox` (new, `None` in `DEFAULTS` -- a truthy default could not be
  told apart from a deliberate choice). Precedence:
  `daily_sandbox=false` -> unsandboxed; otherwise sandboxed UNLESS
  `agent_process_sandboxed=false`; a CONFLICT reports the WEAKER boundary. Absent /
  `None` / unparseable all mean "no opinion", and no opinion is the safe default,
  so a typo can never disable a container.
  `shared.approval.resolve_daily_trust(config, *, repo_path, repo_key,
  session_id) -> DailyTrust` returns the boundary as a receipt;
  `DailyTrust.config_patch()` MUST be applied by the caller for the receipt to be
  true, and `verify_trust_applied(trust, config)` names any disagreement. The CLI
  renders it at run start (`banner_lines()`, the UNSANDBOXED warning first, and
  `quiet` never suppresses it) and writes `logs/{task_id}/trust.json` -- a
  SEPARATE file, because `trace.jsonl` is the kernel's append-only record with
  contiguous sequence allocation and a second writer could corrupt `replay_run`.
  The receipt carries no completion vocabulary at all.
  (3) **The `global` approval scope is DELETED, not renamed.** `APPROVAL_SCOPES` is
  now `once | exact_call | session_path | session_command_prefix`;
  `RETIRED_APPROVAL_SCOPES = ("global",)` keeps the name so a refusal can be
  explained. `ApprovalGrant.matches` no longer returns `True` unconditionally; a
  caller asking for `global` is narrowed to `once` and the audit row says
  "approval accepted once only; scope 'global' is not a scope: …". The user's yes
  is still honoured for the ONE displayed effect (denying it would be wrong, and a
  hard deny runs through the policy engine either way); what cannot happen is a
  grant. `harness/config.py`'s `approval_scopes` no longer lists `global`.
  (4) **An empty command prefix is a configuration error, refused.**
  `EmptyCommandPrefixError` / `ValueError` carrying `EMPTY_PREFIX_REFUSAL`
  ("would approve every command; name the command, or use the 'once' scope"), at
  `PolicyRule.__post_init__`, `ApprovalGrant.__post_init__`,
  `TrustLedger.record` (command-prefix scopes only), and
  `PolicyEngine.record_approval` (a DENY whose reason is the refusal). Defence in
  depth: a force-mutated blank prefix matches nothing, and an unusable grant row
  in a hand-edited journal is dropped on load.
  (5) **ONE prefix matcher, with a boundary.**
  `shared.approval.command_prefix_matches(command, prefix)`: `pytest tests/`
  covers `pytest tests/test_a.py`, `git sta` never covers `git stash`, and an
  empty prefix matches nothing. `harness/agent_kernel/policy.py::_prefix_matches`
  delegates to it (it was a bare `str.startswith`). `cli/commands.py::_command_matches`
  is a SECOND copy that must be made to delegate -- see the requests below.
  (6) **Trust calibration, revocable, opt-in persistence.**
  `shared.approval.TrustGrant` / `TrustLedger` (session- and repo-scoped, bounded
  by `max_grants`, per-grant expiry honoured, `forget()`-able) plus
  `trust_ledger_path` / `load_trust_ledger` / `save_trust_ledger`.
  `cli.interactive._trusted_approver(approver)` wraps ANY surface's approver, so
  calibration is a property of the daily PATH: it reads the ledger before
  prompting and records any non-`once` scope the approver returned. A grant never
  crosses a repository or a session; cross-session persistence is a separate
  DEFAULT-OFF key (`approval_calibration_persist`).
  (7) **New config keys, all additive:** `daily_sandbox` (None),
  `approval_calibration` (None), `approval_calibration_persist` (None),
  `approval_calibration_max_grants` (200, a pure bound), `approval_scope`
  (`session_command_prefix`). The three `None`s are behaviour-neutral: a value in
  `DEFAULTS` is merged into every task and every eval arm, and a real value would
  have silently switched every one of them. `approval_scopes` lost `"global"`.
  (8) **Verified.** `tests/test_ceiling_r2_15_trust.py` -> **49 passed**
  (host-only). `test_agent_kernel` + `test_ceiling_r2_04_daily_default` +
  `test_tool_protocol` + `test_workspace_security` + `test_ceiling_security` +
  `test_difficulty_approval` + `test_config_trace_state` + the new file ->
  **267 passed**. `test_security_regressions` 26 passed / 4 skipped (pre-existing
  Windows symlink skips). `test_daily_driver_evals` + `test_evals_run` +
  `test_evals_tasks` -> **69 passed**. `evals.run --check` 14/14 CLEAN. `ruff
  check` clean on all five owned files; `ruff format` applied to
  `shared/approval.py`, `harness/agent_kernel/policy.py`, and the new test only.
  **Red and honestly attributed (no assertion weakened):**
  `test_cli_command_system.py::TestHeadlessSurface::test_headless_display_output_redacts_session_secrets`
  fails because a session renderer prints the redaction placeholder
  `[REDACTED_SECRET]` through a rich console and rich parses it as markup --
  PROVEN not this round by an A/B that booby-traps every function it added and
  reproduces the failure identically, and by `git diff --stat -- shared/security.py`
  being empty. `test_security_regressions.py::test_r2_11_repeated_unterminated_private_key_blocks_are_linear`
  failed once inside a large combined run (a wall-clock ratio whose first sample
  is 0.00055s) and passes standalone. **No live-provider lane was run and no
  credential was inspected.** The full `evals.run` matrix was not run; only
  `--check`.
  (9) **Cross-terminal requests, all in `cli/AGENTS.md` §6 / `harness/AGENTS.md`
  §4:** (a) `cli/tui.py` (R2-17) -- call `interactive.resolve_session_trust` +
  `render_trust_banner` at run start and wrap `_agent_approve_fn` with
  `interactive._trusted_approver`; (b) `cli/commands.py` -- make
  `_command_matches` delegate to `shared.approval.command_prefix_matches`, and
  add a `/trust` slash command for the revocation door; (c) `harness/agent_loop.py:787-795`
  -- the `legacy_agent` engine still runs BASH through the local subprocess stub
  unconditionally and needs the same resolved-boundary read, failing loud when
  Docker is down; (d) `harness/agent_kernel/strategy.py` -- `process`,
  `process_write_stdin`, `process_kill` and the `start_local_execution` fallback
  still resolve to local execution, so one `_process_sandboxed(cfg)` helper
  would complete the containment story.
- 2026-09-26 (R2-04 - `daily` is the real default engine): **`harness.agent_loop
  .run_agent` no longer overrides the kernel's strategy resolver. An unqualified
  general-agent run now runs the `daily` strategy; `legacy_agent` is reachable
  only by explicit request. The returned result gains two additive keys, one new
  config key restores the old default, and the journal's `strategy_source` stops
  lying. No Boundary 0-5 signature, event kind name, completion status, verifier
  mint, or journal schema changed.**
  (1) **The dispatch decision lives in exactly one function.**
  `harness.agent_loop.resolve_agent_dispatch(config) -> (dispatch, name, source)`
  is the single place this module decides which engine a run uses. It NEVER
  re-implements the resolver's precedence - it calls
  `harness.agent_kernel.resolve_agent_strategy`. `dispatch` is what is handed to
  `SessionController.run_turn(strategy=...)`; it is `None` whenever this module
  has no opinion, which is what "the absence of a strategy is absence" means in
  code. The previous behaviour (`explicit="legacy_agent"` whenever no strategy was
  named) is deleted, and `run_agent`'s docstring no longer contradicts the call
  below it. An unknown strategy name still raises `ValueError` from the resolver
  before any run directory is created; the fail-closed contract is unchanged.
  (2) **Precedence, and it is the only precedence here:** a strategy the caller
  NAMED (`agent_strategy`) always beats the compatibility DEFAULT
  (`agent_default_strategy`), because a key whose name says "default" must not be
  able to overrule an actual choice.
  (3) **New config key `agent_default_strategy`** (`harness/config.py DEFAULTS`,
  value `None`). Absent / `None` / `""` / `"none"` / `"default"` all mean "no
  override", so the `None` entry in `DEFAULTS` is behaviour-neutral and adding it
  switched no task and no eval arm silently - only a PRESENT, non-empty,
  resolvable name changes behaviour. Set it to `"legacy_agent"` to restore the
  pre-0.3.0 default engine for an existing user or a pinned integration. An
  unresolvable value raises rather than falling back, so a typo cannot quietly
  restore the wrong engine.
  (4) **New public `harness.agent_loop.run_agent_legacy(request, repo_path,
  config=None, **kwargs)`** - the dedicated compatibility entry point. It forces
  `agent_strategy="legacy_agent"` and forwards every keyword verbatim to
  `run_agent`; it adds nothing but the pin, so the legacy path is reachable by
  name instead of by accident. The legacy engine is fully supported, not a
  fallback. `AGENT_DEFAULT_STRATEGY_KEY` and `resolve_agent_dispatch` are also
  exported in `__all__`.
  (5) **`AgentResult` (the `run_agent` dict) gains two ADDITIVE keys:**
  `agent_strategy_source` (`"config"` | `"compat_default"` | `"default"`, the
  dispatch source) and `agent_strategy_resolver`
  (`"harness.agent_loop.resolve_agent_dispatch"`, naming the single authority).
  `agent_strategy` is unchanged and still carries the strategy NAME. Consumers
  must treat unknown keys as additive; the status vocabulary is untouched.
  (6) **The journal names the engine and stops mislabelling the source.** The
  kernel's `run_started.strategy_source` and `strategy_selected.source` already
  existed; what changed is that they are no longer corrupted - a default run used
  to record `source="explicit"` because `run_agent` forged an explicit request.
  **Known, documented deviation:** for an unqualified run the kernel row reads
  `source="spec"`, not `"default"`, because `SessionController.run_turn`
  pre-fills `RunSpec.strategy` with the resolver's own fallback before the kernel
  resolves, so the spec branch always matches. Making it read `"default"` needs a
  one-line change in `harness/agent_kernel/kernel.py` (`run_turn`'s
  `_strategy_name(requested or "daily")`), which is not this prompt's file
  ownership. Until then, the authoritative `default` source is the
  `agent_strategy_source` result key, and both are asserted in
  `tests/test_ceiling_r2_04_daily_default.py`.
  (7) **A behaviour-changing default, called out in `CHANGELOG.md`.** A caller
  that relied on the legacy engine without naming it will change engine, trace
  kinds, and on-disk artifacts. `run_agent`'s result is unchanged in SHAPE except
  for (5), and a legacy-protocol caller that needs the old engine must now say
  so - by `agent_strategy`, by `agent_default_strategy`, or by calling
  `run_agent_legacy`.
- 2026-09-26 (R2-08 - kernel model backoff and single context authority): **the
  kernel's model-call path gains the bounded backoff Ceiling-07 recorded as an
  unlanded cross-owner request; `context_compaction_model` is honoured;
  `runtime.model_capabilities` becomes the ONE context-window authority; the
  legacy step loop's cacheable prompt shape is built and gated behind ONE
  explicit key that is NOT defaulted on. No Boundary 0-5 signature, event kind
  name, completion status, or verifier mint changed.**
  (1) `harness/agent_kernel/gateway.py` - additive. `ModelGateway.__init__`
  gains three optional keywords (`recovery=None`, `sleep=None`; plus
  `ModelResponse.model_failure`, a JSON-safe mapping in
  `harness.tool_errors.ModelFailure` vocabulary). New `bind_recovery(recovery)`,
  `has_bound_recovery`, `recovery_report()`; new private `_invoke_with_recovery`
  and `_classify`. `call()` now routes the boundary through
  `harness.tool_errors.ModelRecovery` - **the SAME class and the SAME
  `classify_model_failure` the legacy step path uses, not a second classifier** -
  with the SAME slugs, classes, and terminal-vs-retryable split, and it reads
  the SAME config keys (`max_model_attempts`, `model_retry_base_s`,
  `model_retry_cap_s`). A non-provider exception is therefore still
  `model_internal` + terminal on the kernel path: a `TypeError` in harness code
  is raised on the FIRST attempt however provider-flavoured its message, and
  `KeyboardInterrupt`/`SystemExit` are re-raised untouched (never classified,
  never retried). A retried call is still ONE `ModelResponse`; the call ledger
  row gains additive `attempts` (provider attempts this logical call cost) and
  `model_failure_kind`. `max_model_attempts: 1` is the honest OFF arm and emits
  no `model_recovery` row at all.
  (2) `model_recovery` rows now share ONE payload shape. The gateway's rows
  carry `label: "kernel-model-call"`; the daily strategy's pre-existing
  turn-level row is enriched (additively - `attempt` and `error` are unchanged)
  with `label: "kernel-turn"`, `scope: "turn"`, `step`, `max_attempts`, `kind`,
  `detail`, `status_code`, `retryable`, `terminal`, `backoff_s`, `action`, so a
  consumer reads one vocabulary and distinguishes the two axes. The kernel row
  arrives through `JournalTraceLogger(self.events)`, so it is on the run's own
  `trace.jsonl` like every other kernel event.
  (3) `DailyCodingStrategy` - additive only: `_bind_model_recovery()` (one
  recovery per run, so its counters span the run), `model_recovery_report()`,
  `_primary_summary_model_name()`, `_summary_gateway()`, `_absorb_spend()`.
  `RunResult.metadata["model_recovery"]` is new and additive, alongside the
  existing `metadata["context"]`. `context_compacted` / `compactions.jsonl` rows
  gain additive `compaction_model` (the model that ACTUALLY summarized - this is
  a CHANGE of value, not just a new key), `compaction_model_configured`,
  `compaction_model_honoured`, `compaction_model_method`.
  `context_compaction_model` is now the model the primary summarizer calls. If
  the run's own boundary is not reachable the key is NOT honoured, the run's own
  model summarizes, and the receipt says so - a receipt must never imply a
  configured model was used when it was not.
  (4) `harness/agent_kernel/budget.py` - `budget_from_config` now READS
  `runtime.model_capabilities.resolve_context_window` instead of resolving a
  second window. **The authority is a CEILING; a config key may lower the window
  but never raise it past it**, and the authority's `fallback` rung (a refusal to
  guess) never shrinks a declared budget, so an unknown model keeps whatever the
  run declared. `ContextBudget` gains additive `window_source`, `window_model`,
  `window_declared`, `window_authority` (all surfaced in `as_dict()` with
  `window_authority_applied`). `DEFAULT_CONTEXT_WINDOW` (32768) matches
  `harness.config.DEFAULTS["context_window_tokens"]`, so an unconfigured run is
  byte-identical to before the authority was consulted.
  (5) `harness/prompts.py` - NEW `step_system_text(messages)`, the ONE
  dispatcher-safe way to read a step prompt: it returns byte-identical text for
  the historical single-system-message shape and for
  `render_step_messages`'s `[system(frozen), system(per-turn), user]` split, so a
  scripted model can be migrated before or after the split. Nothing else in
  `prompts.py` changed.
  (6) `harness/config.py` - one new key, `"step_prompt_cache_split": None`. It
  is `None`, NOT `False`, and that is load-bearing: a value in `DEFAULTS` is
  merged into every task, so a truthy default would silently change every run and
  every eval arm's message shape. Absent / `None` / `False` all mean the
  historical single interpolated system message. `harness.core.run_step` reads it
  and, when true, calls `prompts.render_step_messages`; either way it now writes
  a `step_prompt_cache` trace row (new, additive) carrying the prefix digest,
  breakpoint index, prefix/suffix message counts, and prefix size.
  **`step_prompt_cache_split` is NOT enabled anywhere.** The conversion is one
  key away and is blocked on migrating the scripted models that dispatch on the
  FIRST system message - see `harness/AGENTS.md` for the measured list and the
  reason. `harness.config.DEFAULTS["require_edit_digest"]` was added by R2-06
  while this prompt was in flight and is NOT this prompt's change.
  (7) Verification: NEW `tests/test_ceiling_r2_08_backoff_context.py` -> 25
  passed. `tests/test_recovery_steering.py test_tool_protocol.py
  test_config_trace_state.py` -> 110 passed;
  `tests/test_agent_loop.py test_prompt_cache_cost.py` -> 94 passed;
  `tests/test_evals_run.py test_evals_tasks.py` -> 35 passed;
  `tests/test_cli_runview.py test_modes.py` -> 157 passed, 2 skipped;
  `python -m evals.run --check` -> 14/14 CLEAN; a real
  `python -m evals.run --arms baseline,no_lint --tasks bug01_wrap --quick` ->
  CLEAN, exit 0. `ruff check` and `compileall` clean on every owned file.
  **NOT run / NOT clean, reported honestly:** `tests/test_agent_kernel.py` -> 59
  passed / 2 failed and `tests/test_context_budget_engine.py` -> 8 passed / 10
  failed, ALL of them the same root cause and PROVEN not to be this round: R2-06
  flipped `harness.config.DEFAULTS["require_edit_digest"]` to `True` at ~19:06 on
  2026-09-26 (it was off), which refuses every scripted `write`/`edit` whose file
  the run never read. Controlled A/B probe (outside the repo, patching only the
  registry's read of the key) turns `test_context_budget_engine.py` from 8/18 to
  **17/18** and turns `test_agent_kernel.py::test_safe_backend_overwrites_and_
  rolls_back_invalid_syntax` green with no other change. The one A/B-unreachable
  case is a subprocess hard-kill test the in-process probe cannot patch.
  `python -m evals.run --quick` did NOT complete inside a 30-minute window in
  this session and is **not claimed**. No Docker lane beyond the single eval task
  above, and **no live-provider lane was run and no credential was inspected.**
- 2026-09-26 (R2-10 - snapshots honour .gitignore, declare a scope, keep a
  disk budget, share immutable content, and prune on a policy): **NEW
  `execution/snapshot.py`; one additive public predicate in
  `execution/workspace.py`; one additive keyword on
  `execution.test_selection.select_tests`; three additive read-mostly
  functions in `memory/paths.py`; one additive `DoctorCheck` row in
  `cli/doctor.py`. No Boundary 0-5 signature, event kind, serialized field,
  completion status, or verifier mint changed, and `harness/config.py`,
  `harness/core.py` and `harness/editor.py` were NOT edited by this prompt.**
  (1) **The ignore decision is GIT's, not a second matcher.** One batched
  `git check-ignore --no-index -z --stdin` subprocess per tree (plus a
  `git rev-parse --show-toplevel` probe, because `check-ignore` outside a
  repository exits 1 with no output, which is byte-identical to "nothing is
  ignored" and would let a receipt claim git semantics for a directory git
  never looked at). Nested `.gitignore` files, negations, `**` and
  `core.excludesFile` are honoured by git itself. The query is rebased onto the
  work tree's top level, so a source that is a SUBDIRECTORY of a repository
  inherits that repository's rules. When git cannot answer, the fallback is the
  module's EXISTING artifact/VCS set via the new
  `execution.workspace.is_generated_path` (one addition to that file: the
  non-raising, walk-time form of the `_is_artifact` set `is_protected_path`
  already uses), and `IgnoreRules.source` records which of the two actually ran.
  A test reads this module's source and fails if a gitignore pattern table ever
  appears here.
  (2) **A declared scope is `Task.config["task_scope"]`, opt-in by key
  presence.** `TaskScope.from_config` accepts one relative path or a sequence;
  absent/`None`/`False`/`""`/`[]` resolve to the WHOLE repository (an absent
  key must leave every existing run byte-identical), while absolute, drive,
  traversing, whitespace-only, non-string, and non-existent values RAISE
  `SnapshotScopeError` rather than widening. The scope bounds the snapshot, the
  path filter a retrieval caller uses, and the test selection; an edit that
  escapes it is REPORTED by `scope_verdict` / `SnapshotReceipt.scope` and is
  named in the receipt. ONE documented exception: a bounded set of ROOT
  configuration files (`pyproject.toml`, `pytest.ini`, the root `conftest.py`,
  the dependency manifests -- `DEFAULT_SCOPE_CONFIG_FILES`) is always carried
  and always reported as `config_files`, because a `pristine` copy without them
  makes a scoped verifier run a DIFFERENT suite from the baseline.
  (3) **The budget is measured before a byte is written.** `plan_run_snapshot`
  walks and stats without writing; `create_run_snapshot` then enforces
  `enforce_budget` and only then writes. Two independent refusals plus a third:
  the measured upper bound vs `snapshot_budget_bytes`, the same bound vs free
  space minus `snapshot_reserve_bytes` (unmeasurable free space is
  `free_space_unknown`, a refusal), and `plan_incomplete` for a walk that hit
  `snapshot_plan_budget_s` / `MAX_WALK_ENTRIES`. The ceiling is checked against
  the PRE-link byte count, which is the upper bound -- sharing can only reduce
  the real figure. `BUDGET_REASONS` is a closed exported set.
  (4) **Content sharing is safe because the write path is separated
  STRUCTURALLY, not by convention.** `SharedStore` is a content-addressed
  `<store>/blobs/<aa>/<digest>` tree; `pristine/` entries are hardlinks into it
  and are chmod'ed read-only, and `work/` is ALWAYS a private byte copy. A
  bind-mounted sandbox truncates in place, so linking `work/` would silently
  rewrite the diff baseline and every other run sharing the inode. A store that
  cannot link falls back to copying and `SnapshotReceipt.shared_disabled_reason`
  names why; nothing is ever reported as shared that is not. Blobs are written
  to a temp file and `os.replace`d so a crash cannot leave a truncated blob.
  (5) **Retention prunes run DIRECTORIES and is narrow by test.** A candidate
  must satisfy `is_run_directory` (it carries `trace.jsonl`/`state.json`/
  `plan.json` or a `pristine`/`work` pair); everything else in the log root is
  reported in `PruneReport.skipped` and left alone. Age + total bytes +
  keep-latest compose: a run is removed only when BOTH the age window and the
  byte budget select it AND it is not among the newest `keep_latest`. Age and
  byte triggers also apply to the store. `force_rmtree` is Windows-read-only
  safe (`onerror`/`onexc` chosen by version), and one locked directory is
  reported while the rest still prune. The policy shape is
  `shared.retention.RetentionPolicy`, not a second vocabulary; the bounded
  defaults and every override key live in `execution/snapshot.py`, NOT in
  `DEFAULTS`.
  (6) **`execution.test_selection.select_tests` gained ONE additive
  keyword-only `scope=None`.** The scope narrows the CANDIDATE TEST SET before
  any graph reasoning, so the soundness argument is unchanged. An empty in-scope
  set yields `strategy="scope_empty"` and `is_empty=True` so the caller's
  existing empty-selection fallback still fires; an out-of-scope selection is
  reported in `TestSelection.out_of_scope`; an explicit `target_test` outside
  the scope is still selected (the declared target outranks the scope, and
  dropping it would change what the verifier is asked to run). `to_dict`/
  `from_dict` round-trip the two new fields with absent-key defaults, so a
  selection persisted before this change still loads.
  (7) **NOT WIRED, recorded as a handoff.** No production call site passes
  `config`/`scope`/`store` to `create_run_snapshot`; `harness/editor.py`'s
  `snapshot()` is still the historical two-argument `shutil.copytree`, and
  `harness/core.py` still calls it at `core.py:493-494`. The
  `python -m execution.snapshot` surface is runnable today and is what the
  required tests drive. The exact edits are in `execution/AGENTS.md` under
  "Cross-terminal requests".
  (8) **MEASURED, this repository, real host.** `python -m ruff check` clean on
  all six touched files; `ruff format --check` clean on the two new files.
  `python -m pytest tests/test_ceiling_r2_10_snapshot_disk.py -q -p
  no:randomly` -> **57 passed** (host-only: real `git check-ignore`, real
  `os.link`, real `rmtree`; no Docker, no model, no network).
  `tests/test_editor_prompts.py tests/test_cli_power_tools.py` -> **80 passed,
  1 skipped**; `tests/test_ceiling03_sessions.py` -> **31 passed** (254s);
  `tests/test_ceiling08_verification.py` -> **78 passed**;
  `tests/test_workspace_security.py` -> **53 passed**;
  `tests/test_config_trace_state.py` -> **14 passed**;
  `python -m evals.run --check` -> **14/14 CLEAN**. The single edit outside this
  prompt's files is disclosed: `tests/test_cli_power_tools.py`'s doctor count
  pin moved from `8` to `9` and gained a per-row field assertion, because the
  new check is a registry entry the count necessarily counts.
  A real `plan_run_snapshot` over THIS repository (which is pathological: a
  bare `os.walk` of it takes **362s** and visits 291,174 files / 150,361
  directories) measured, with a 45 s planning budget: `ignore_source=git+generated`,
  **530.8 MiB across 16,408 files identified as generated and never copied**,
  351 files / **49.1 MiB** that would be written for the `pristine`+`work` pair,
  and the verdict **`plan_incomplete` -- a refusal**, because the bounded walk
  could not finish. The largest KEPT files are named in the receipt
  (`Temp/opencode/scan-smoke{2,3,4}/_code-graph/.../graph.json` at ~2.4 MiB
  each, `.shots/final-hero.png`), which is the diagnostic the prompt's `doctor`
  section now surfaces. `python -m memory.paths.snapshot_disk_receipt` on the
  real `logs/` root measures **155.5 MiB across 45 run directories, 151.6 MiB of
  it `pristine/`**, i.e. the reference copy is ~98% of the artifact tree -- the
  split `SharedStore` halves. No live-provider lane and no Docker lane were run;
  neither is claimed.
- 2026-09-26 (R2-11 - the quadratic redactor is now linear): **`shared/security.py`
  only. No Boundary 0-5 signature, event kind, serialized field, completion
  status, or redaction OUTPUT changed. `harness/config.py` was NOT edited and
  nothing was added to `DEFAULTS`.**
  (1) **No contract change; an additive surface.** `redact_text(value, secrets)`
  keeps its exact signature and its exact output for every input. Added:
  `RedactionScan` (frozen dataclass with `to_dict()` / `summary()`),
  `redact_text_scanned(value, secrets) -> (text, RedactionScan)`,
  `redaction_scan(value, secrets) -> RedactionScan`, and the constants
  `URL_PREFIX_HOT_RUN` (256), `REPEATED_CHAR_HOT_RUN` (64),
  `REDACTION_SCAN_SPAN_CAP` (4096), `REDACTION_SCAN_SPAN_OVERLAP` (512). All
  four constants and all three names are in `shared.security.__all__`.
  (2) **The defect, and a correction to the filed report.** The T11 report in
  `harness/AGENTS.md` attributed the quadratic to "repeated characters", on the
  evidence that a mixed 40k payload cost 0.03s. That evidence was too narrow:
  the class is any long run of `[a-z0-9+.-]` (the greedy prefix of the URL
  rule), and `("abcdefgh" * 5000)` — mixed text without spaces — cost **28.4s**
  where the "fine" mixed payload cost 0.047s. Per-rule isolation named
  `_URL_USERINFO` as the sole culprit (4.77s of 5.11s at n=16000) and found a
  SECOND independent quadratic class in the PEM rule's lazy `.*?` (640
  unterminated BEGIN blocks, 20 480 chars → 0.218s).
  (3) **The fix is a linear pre-scan plus per-rule required-literal gates.**
  `_url_userinfo_candidates` enumerates the only spans a URL-userinfo match can
  open in (maximal `[a-z0-9+.-]` runs immediately followed by the literal
  `://`) in one C-level pass; `_redact_url_userinfo` replaces the quadratic
  `sub` with a marker walk whose class probes REUSE the original rule's own
  character classes, so the gate cannot drift from the rule it gates.
  `_SECRET_PATTERN_GATES` is `(slug, required_literal_or_None, pattern)`;
  `_SECRET_PATTERNS` remains the plain pattern tuple in the pre-R2-11 order
  because `contains_secret` and `harness/context_compiler.py` iterate it.
  Skipping a rule whose required literal is absent from the whole text is a
  proof it had no match, never a heuristic.
  (4) **The scan cap is a REPORTED condition, and it is not a bypass.** The cap
  bounds the shape-statistics walk per line; the security gate reads the whole
  text, so exceeding the cap can only DISABLE a skip, never enable one. The
  round allowed either "a secret straddling the cap is redacted" or "not
  redacted but reported"; **the safe branch is pinned by test**, and
  `RedactionScan.cap_reached` / `capped_lines` / `scanned_chars` report the cap
  either way. Statistics are opt-in: `redact_text` builds no receipt, and the
  internal helper returns `None` for the scan unless statistics were requested,
  so a zeroed counter cannot be published as a measurement.
  (5) **MEASURED.** `redact_text("y" * n)`, old → new: 2 000 0.0707s → 0.0010s;
  4 000 0.2896s → 0.0019s; 8 000 1.2400s → 0.0044s; 12 000 3.0200s → 0.0061s;
  16 000 5.1070s → 0.0080s; 40 000 ~32s (interpolated) → 0.0201s. A 20× input
  now costs 19× the time, was ~450×. `("abcdefgh" * 5000)`: 28.4s → 0.0159s.
  No measured slowdown on ordinary text (fastest-of-5, same process, both
  implementations): empty 0.80×, one 40-char log line 0.81×, 1 000 log lines
  0.86×, trace rows 0.18–0.20×, 200 URLs with userinfo 1.03×.
  (6) **Equivalence is measured.** The test file transcribes the pre-R2-11 body
  verbatim and diffs it against the current one over exhaustive short strings,
  8 000 seeded randoms, and planted-secret cases: 0 divergences. An ad-hoc run
  of the same differential over 69 638 inputs also reported 0 mismatches.
  (7) Verified: `tests/test_security_regressions.py` 26 passed, 4 skipped (the
  pre-existing Windows symlink cases); the shared consumer selection 253
  passed, 2 skipped; `python -m evals.run --check` 14/14 CLEAN; `--quick`
  CLEAN, 0 regressions, 40/40 across all 8 arms; ruff check clean on both
  owned files. `tests/test_context_budget_engine.py` fails 10/18 and is **not**
  from this round — the identical 10 fail with the pre-R2-11 redactor patched
  back in, and both that file and `tests/test_ceiling_r2_08_backoff_context.py`
  are untracked in-flight work from the context-compiler terminal. Both of
  those files carry a fixture workaround for this very defect which is now
  obsolete. `tests/test_ceiling_r2_08_backoff_context.py` failed 2/25 once and
  then passed 25/25 four times running; recorded as flaky, cause not identified.
  `tests/test_agent_sdk.py::test_cancel_calls_kernel_directly_and_preserves_status`
  failed once on an `entered.wait(2)` thread-startup timeout at line 153; 16
  interleaved runs gave 8/8 passed with this redactor and 6/8 passed with the
  pre-R2-11 one, so it is flaky under host load in both directions, not a
  failure of this round. Neither assertion was weakened.
  Not counted as a pass. No Docker lane and no live-provider lane was run and
  neither is claimed.
  (8) **Handoffs, all in `shared/AGENTS.md` and none applied here:**
  `harness/context_compiler.py` re-implements a redaction pass with a
  deliberately different BOUNDED URL rule and should be reconciled with the one
  policy Ceiling-13 closed as gap G34; `memory/decision_store.py:81-82` carries
  both quadratic shapes; `harness/trace.py` is the natural place to emit
  `RedactionScan.summary()` on the journal-write path; and `shared/security.py`
  cannot import `shared/tracing.py` (the dependency is the other way), which is
  why the receipt is a return value rather than a trace event.
- 2026-09-26 (R2-01 - wire the verification-intelligence layer into
  production): **NEW `execution/verification_gate.py` plus ONE additive
  keyword-only parameter on Boundary 1, `verify(..., intelligence_config=None)`.
  No existing signature, return type, serialized field, completion status, or
  the existing target+regression+not-flaky mint was changed, weakened, or
  re-derived. `shared/types.py`, `harness/core.py`, `harness/editor.py`,
  `harness/agent_loop.py`, and `harness/config.py` were NOT edited by this
  prompt.**
  (1) **The seam.** `execution/verify.py` gains exactly one delegation point,
  `_intelligence_delegate(...)`, called as the FIRST statement of `verify()`:
  `if any(key in intelligence_config for key in _INTELLIGENCE_CONFIG_KEYS):
  return run_intelligent_verify(...)` else the pre-existing body runs
  BYTE-IDENTICALLY. This mirrors the Ceiling-14 `provider_gateway` seam in
  `runtime/model_router.py` exactly, including its load-bearing detail: the KEY
  tuple `_INTELLIGENCE_CONFIG_KEYS` lives in `verify.py` (the seam holder) and
  the pipeline module is imported LAZILY, so the key-presence question is
  answerable even when the pipeline module is broken, and an unconfigured caller
  never imports the intelligence subgraph at all.
  `execution.verification_gate.INTELLIGENCE_CONFIG_KEYS` re-exports that tuple,
  so there is exactly one literal. `execution/verification_intelligence.py` is
  NOT on this path and was not modified.
  (2) **Activation is key-PRESENCE, never a value, and NOTHING is in
  `DEFAULTS`.** The 15 keys are `verification_intelligence`,
  `verification_spec_root`, `verification_require_spec`,
  `verification_max_obligations`, `verification_held_out_suite`,
  `verification_held_out_root`, `verification_held_out_cases`,
  `verification_held_out_seed`, `verification_independent_judge`,
  `verification_gap_threshold_points`, `verification_run_dir`,
  `verification_task_id`, `verification_run_id`,
  `verification_held_out_runner`, `verification_confirm_runner`. None of them
  appears in `harness/config.py::DEFAULTS`, and that is test-pinned: a default is
  merged into every task config, so one would switch every task and every eval
  arm at once. `{"verification_require_spec": False}` therefore still OPTS IN —
  the operator wrote the key and turned that gate off inside the pipeline.
  `resolve_settings(config) -> Settings` holds the pipeline's own bounded
  internal defaults (`DEFAULT_MAX_OBLIGATIONS = 8`,
  `DEFAULT_HELD_OUT_SEED = 0`, `DEFAULT_GAP_THRESHOLD_POINTS = 5.0`), and a value
  that had to be coerced is reported as a `config: ... is not usable; N was
  used` note rather than silently replaced.
  (3) **Three rungs, and every verdict names the one that produced it.**
  `RUNGS = ("baseline", "spec", "independent")`; gates
  `baseline_target_and_suite` (rung `baseline`), `spec_intact` and
  `spec_obligations` (rung `spec`), `independent_evidence` (rung
  `independent`). Each `GateVerdict(rung, name, passed, mandatory, reason)`
  reaches a reader three ways: one `reports` row per gate with an added
  `rung`/`gate`/`mandatory` (beside the existing `outcome`/`source`/
  `confidence`/`notes` keys `execution.verify._report` already emits), a
  `## verification-gate` block appended to `VerificationResult.raw_output` with
  one `  rung=<rung> gate=<gate> passed=<bool> mandatory=<bool> reason=<...>`
  line per gate, and one unified-trace event `verification_gate`
  (`module="execution"`). A verdict carrying a rung outside `RUNGS` is dropped
  and reported rather than emitted, so a receipt can never name a mechanism this
  layer does not implement.
  (4) **The ledger loads where the obligation set is needed.** `_load_spec(root)`
  runs inside the `spec` rung and nowhere else, with
  `root = verification_spec_root or verification_run_dir or repo_path`. The
  `spec_obligations` gate then RE-RUNS each sealed item's declared test
  references as its own sandboxed pytest selection and requires each to be
  collected AND passing; an obligation naming a renamed or deleted node id
  therefore cannot satisfy itself (`execution.result_parsing` already refuses to
  call a vanished selection a pass). A missing, unparseable, unsealed, or
  required-but-absent ledger is a FAILING gate carrying the reason, plus a
  `degraded:` note, and is never a silent pass. A sweep bounded by
  `verification_max_obligations` that skipped obligations is a REFUSAL naming
  them, because an unchecked obligation is where a shrunken spec would hide.
  (5) **The judge is additional evidence, opt-in per run, and never
  authoritative.** It runs only when `verification_independent_judge` or a suite
  source is present. An unresolvable suite is a FAILING gate with the reason,
  never an absent gate. The default runner is the Docker sandbox
  (`_sandbox_held_out_runner`), which stages the suite inside the judge's own
  clean copy in a SHORT system-temp directory — the depth that would otherwise
  produce Docker Desktop `EIO` reads that look like code failures — and retries
  ONCE only on a crash-shaped `error` outcome, never on a genuine test failure.
  A judge verdict can only ever REFUSE; there is no code path that sets a
  boolean back to `True`.
  (6) **How a verdict reaches the fail-closed mint.**
  `harness.core.run_task` mints `status="success"` on exactly
  `target_test_passed and regression_passed and not flaky`, and
  `shared.types.VerificationResult` (not this round's file) carries no field for
  gate rows. So `plan_fold` CLEARS `target_test_passed` and nothing else, and
  only when a MANDATORY rung refused; `regression_passed` and `flaky` are never
  touched. The cleared field is a statement about the CLAIM, not about the
  tests, which is why the receipt travels in the same `raw_output` block a reader
  of the trace already has. `applied`/`folded_fields` distinguish "the
  intelligence fold blocked this" from "the baseline already refused", so the
  block is never read as a suite failure. The additive
  `VerificationResult` field that would remove the need for the fold is filed as
  a cross-terminal request in `execution/AGENTS.md`, not done here.
  (7) **Two documented declines, both observable.** `run_intelligent_verify`
  returns `None` — meaning "continue on the pre-existing path" — exactly when no
  intelligence key is present, and when `final_gate=False` (the repair loop's
  non-gating inner gate must stay structurally incapable of looking like a
  completion claim). The second emits a `verification_gate_declined` trace event
  with `reason="non_gating_inner_verification"`, so the decline is never silent.
  (8) **A pipeline that cannot be imported is not a silent fallback.** With an
  intelligence key present and the import failing, `_intelligence_delegate`
  returns a refusing `VerificationResult` plus an `intelligence_unavailable`
  receipt row naming the reason, rather than serving the weaker baseline.
  (9) **Public surface of `execution.verification_gate`:** `RUNGS`,
  `RUNG_BASELINE`/`RUNG_SPEC`/`RUNG_INDEPENDENT`,
  `GATE_BASELINE`/`GATE_SPEC_INTACT`/`GATE_OBLIGATIONS`/`GATE_INDEPENDENT`,
  `INTELLIGENCE_CONFIG_KEYS`, `GateVerdict`, `IntelligenceDecision`, `Settings`,
  `present_keys(config)`, `intelligence_requested(config)`,
  `resolve_settings(config)`, `evaluate(...)`, `plan_fold(result, decision)`,
  `attach_receipt(result, decision)`, `apply_fold(result, decision)`,
  `raw_output_block(decision)`, `receipt_rows(decision)`, and
  `run_intelligent_verify(...)`. The seam inside `verify.py` reads
  `execution.verify._autodetect_test_command` for the suite command the
  obligation sweep composes onto; that private cross-reference is the intended
  one-module-family seam and is declared here rather than forked into a second
  language detector.
  (10) **Not wired at a production call site, honestly.** No caller in
  `harness/`, `execution/`, `evals/`, `cli/`, or `runtime/` passes
  `intelligence_config` today, so the parameter is `None` on every production
  call and the seam is unreachable outside this round's tests. Activating it is
  a one-line change at each `verify()` call site plus the `DEFAULTS` decision;
  both are filed in `execution/AGENTS.md` as cross-terminal requests because
  `harness/config.py` and `harness/core.py` belong to other prompts.
  Verified: `python -m pytest tests/test_verification_gate_wiring.py -q -p
  no:randomly` -> **27 passed** (real Docker: the suites, the obligation sweep,
  and the held-out judge all run in containers). `tests/test_verify.py
  tests/test_verify_js.py tests/test_feedback.py tests/test_git_output_rationale.py`
  -> **124 passed**; `tests/test_ceiling08_verification.py` -> **78 passed**;
  `tests/test_sandbox.py tests/test_e2e_run_task.py` -> **89 passed** (this lane
  was 88 passed + 1 failed for a `ToolLoopGuard` drift that another terminal has
  since fixed). `python -m evals.run --check` -> **14/14 CLEAN**. Ruff check and
  format clean on both new files. One pre-existing failure in
  `tests/test_daily_driver_evals.py::test_quick_matrix_runs_real_comparison_with_receipts`
  (`feature_evidence.status == "failed"`, the `agent_fetch_enabled` probe, 26/28
  feature arms) is NOT from this round: item (10) above is the proof, since the
  failing probe runs in a `python -m evals.daily_driver` subprocess that no
  production config can reach with an intelligence key. Not counted as a pass.
  No live-provider lane was run and no credential was inspected.
- 2026-09-26 (R2-03 - test configuration is protected, and its EFFECT is
  measured): **NEW `harness/test_config.py` adds three additive keyword-only
  parameters to `harness.editor.check_edits` and widens what
  `harness.editor.is_protected` refuses. No Boundary 0-5 signature, event kind,
  serialized field or completion status was changed, removed or renamed, and
  the verifier's success mint is untouched. `harness/core.py`,
  `harness/agent_loop.py`, `execution/verify.py` and `harness/config.py` were
  NOT edited by this prompt.**
  (1) `harness/test_config.py` (NEW) - the per-language protected
  test-configuration surface table, the declared-intent rule, and the
  effective-configuration resolver. Public surface:
  `register_language_surfaces(language, surfaces, *, replace=False)`,
  `language_surfaces(language=None)`, `test_config_patterns(language=None)`,
  `classify_test_config_path(rel_path) -> Optional[TestConfigSurface]`,
  `is_test_config_path(rel_path) -> bool`,
  `declared_test_config_change(config) -> Optional[str]`,
  `resolve_effective_test_config(root, *, target=None, extra_paths=(),
  test_command=None) -> ResolvedTestConfig`,
  `diff_effective_test_config(baseline, work) -> dict`,
  `test_config_guard(pristine_dir, work_dir, *, config=None,
  changed_paths=(), target_test=None) -> TestConfigVerdict`, and
  `relaxation_slugs(tokens) -> list[str]`; types `TestConfigSurface`,
  `ResolvedTestConfig`, `TestConfigReceipt`, `TestConfigVerdict`; constants
  `TEST_CONFIG_CHANGE_KEY = "test_config_change"`, `WHOLE_FILE`, `TABLE_SCOPED`,
  `TEST_CONFIG_CHANGED_EVENT = "test_config_changed"`, `TEST_CONFIG_EVENT =
  "test_config"`, `SCHEMA_VERSION = 1`.
  Whole-file surfaces: `conftest.py` (ANY directory, which is how pytest scopes
  it), `pytest.ini`, `sitecustomize.py`, `usercustomize.py`, plus the js / ruby
  / php / dart / go / .NET runner configs. TABLE-scoped surfaces, because those
  files are dual-purpose and only their test tables are the test contract:
  `pyproject.toml` (`tool.pytest.ini_options`), `setup.cfg` (`tool:pytest`),
  `tox.ini` (`pytest`), `package.json` (`jest`, `scripts.test`), `angular.json`
  (`test`), `pom.xml` / `build.gradle*` (`test`). `Cargo.toml` is deliberately
  NOT a surface; `register_language_surfaces` is the seam for R2-12 to add
  Go/Rust runners without editing this module.
  (2) `harness/editor.py` - `is_protected` now also refuses any WHOLE-FILE
  test-config surface, independent of `config["protected_paths"]` (so an empty
  pattern list still protects `conftest.py`). `check_edits` gains three
  ADDITIVE keyword-only parameters - `config`, `trace`, `report` - and runs
  the guard BEFORE the configured-glob loop, so a table-scoped violation gets a
  message naming the real reason and a declared change is not then refused a
  second time by the generic rule. New re-exports: `test_config_patterns`,
  `extended_protected_patterns`, `declared_test_config_change`. Every existing
  three-positional-arg call site is byte-identical when the new params are
  omitted.
  (3) The DECLARED INTENT is `Task.config["test_config_change"]` (a reason
  string, or `{"reason": ...}`). Absent / `None` / `False` / `""` means not
  declared, and the issue text is deliberately NOT consulted: prose in the task
  description is reachable by the same agent the guard constrains. The key is
  intentionally ABSENT from `harness/config.py` `DEFAULTS` (an owner may add a
  `None` entry for discoverability - that is behavior-neutral because `None`
  already reads as not-declared), and there is deliberately NO key that
  disables the gate. A declared change still emits its receipt: it is a
  recorded intent, not a silent bypass.
  (4) Two NEW trace events, both additive: `test_config` (the receipt, on EVERY
  gate call, so a clean run is distinguishable from a run that never looked)
  and `test_config_changed` (the same receipt whenever the effective
  configuration differs from the baseline's, declared or not), carrying the
  resolved before/after, the changed paths/fields, and named relaxations
  (`addopts_selector`, `addopts_ignore`, `addopts_deselect`, `addopts_plugin`,
  `addopts_ini_override`, `addopts_collect_only`, `addopts_last_failed_only`,
  `scope_narrowed`, `inifile_changed`, `conftest_added|removed|modified`, ...).
  A guard that raises records `test_config_guard_failed` and does not block; a
  guard that cannot RESOLVE still refuses a file-level violation, because that
  verdict needs no resolution.
  (5) The REFUSAL is already live on the production path: `run_task` already
  calls `editor.check_edits` at `harness/core.py:1291` and `:2659`. The RECEIPT
  is not yet in a run's trace or result - that needs the one-line hand-off
  (`config=cfg, trace=trace, report=...`) recorded in `harness/AGENTS.md`
  section VEX-CEILING-R2-03 section 5, along with the declaration check for
  the `harness/core.py:608-612` / `:2479-2483` and `harness/agent_loop.py`
  tool-layer sites and a `None` DEFAULTS entry.
  (6) Cost and honesty: resolution is O(changed paths + directory depth) and
  never re-walks a repository; the `conftest` chain is resolved for the
  declared target test and `conftest_scope` records whether it was a
  `target_chain` or `root_only` resolution. The resolver READS configuration
  and never runs the test runner, so it is evidence about the declared
  configuration rather than about what pytest would collect.
  (7) Verified: `tests/test_ceiling_r2_03_config_guard.py` 78 passed (77 host
  + 1 real Docker, incl. a REAL `harness.core.run_task` drive in which a
  scripted model defuses the only test file through a new `conftest.py` and the
  task FAILS with `final_edit_validation_failed` naming the test
  configuration); `tests/test_e2e_run_task.py` 28 passed against the real Docker
  daemon; the editor's neighbour selection 198 passed, 1 skipped; a
  `protected_paths`-consuming selection 132 passed, 1 skipped;
  `python -m evals.run --check` 14/14 CLEAN and `--quick` 40/40 CLEAN, 0
  regressions. The full Docker prompt matrix and any live-provider lane were
  not run and are not claimed.
- 2026-09-26 (R2-02 - the flake gate becomes capable of firing): **NEW
  `execution/flake_gate.py` splits the two meanings `rerun_for_flake_check`
  carried, and adds a THREE-valued flake verdict. No Boundary 0-5 signature
  was changed and nothing was REMOVED or RENAMED; the new module has no live
  call site yet (see "not wired" at the end of this entry).**
  (1) **The defect, stated arithmetically.** `harness/config.py` shipped
  `baseline_reruns = 1`; `execution/verify.py` computed
  `run_count = 1 if not target_test else max(1, rerun_for_flake_check)`, so
  `run_count` was 1; `flaky = len(set(outcomes)) > 1` over a 1-element list is
  unsatisfiable. On the default configuration the flake gate could NEVER fire:
  a test that passed once and failed on the third run was reported as a clean
  non-flaky pass. One number was doing two jobs - "how many target runs" and
  "do we detect flakes" - and the second job was silently reduced to "no".
  (2) **The split.** A BASELINE asks only "was this already broken?", which one
  run answers, and it is paid on every task, so
  `DEFAULT_BASELINE_REPETITIONS = 1` and it stays 1. Flake detection is a
  property of the POST-FIX run - the run that mints a completion claim - and
  `DEFAULT_POST_FIX_REPETITIONS = 2`, the smallest number of observations that
  can distinguish "stable" from "not stable".
  (3) **`flake_verdict(repetitions, outcomes) -> FlakeVerdict` is the public
  entry point.** `flake_check` is three-valued and closed in
  `FLAKE_CHECK_VALUES`: `flaky_detected` (>=1 differing outcome observed),
  `not_flaky` (>=2 repetitions, all identical), and `not_run` (fewer than
  `MIN_REPETITIONS_FOR_DETECTION = 2` repetitions observed). `not_run` exists
  because `flaky=False` reads as "we checked and it was stable"; with one
  repetition that is a lie. `flaky` remains a plain bool with its historical
  value so no existing consumer changes behaviour, and
  `FlakeVerdict.detection_possible` is what a consumer must require before
  claiming stability was shown.
  (4) **The verdict is fail-closed against its own caller**: it is derived
  from the number of outcomes OBSERVED, never the number requested, so a
  caller that asks for 3 repetitions and reports 1 gets `not_run` rather than
  a manufactured "not_flaky".
  (5) **Timeout stays a third outcome.** `pass`/`fail`/`timeout` are
  re-exported from `execution.result_parsing` (one vocabulary in the tree),
  timeout is decided first, a pass-then-hang mix is `flaky_detected` rather
  than a stable pass, and a run that times out every time is `not_flaky` with
  `timed_out=True` - consistently broken, not intermittently broken.
  (6) **Config keys (additive, and the reason a key is not just a value).**
  `harness/config.py` should gain `post_fix_reruns` (2) and
  `flake_repetitions_cap` (10); `baseline_reruns` (1) keeps its name and is
  REPURPOSED to mean the baseline count. Lookup is by KEY MEANING, not
  truthiness: `repetitions_for_stage` treats an absent key as the stage
  default and an explicit `0` as a deliberate "one run, no detection", because
  a truthiness check would collapse the two and silently re-enable detection a
  caller switched off. `MAX_REPETITIONS = 10` is a hard ceiling a caller may
  tighten but not exceed: wall-clock is linear in this number and each
  repetition can cost a full `verify_timeout_s`.
  (7) **Auditability.** `FlakeVerdict.to_dict()` / `FlakeRun.to_dict()` carry
  `repetitions`, `requested_repetitions`, `observed_outcomes`,
  `distinct_outcomes`, `timed_out`, `detection_possible` and the measured
  `elapsed_s`, and the receipt is JSON-serializable so the claim is
  reconstructable from a trace row. `attach_evidence(result, verdict)` records
  `flake_check` / `repetitions` / `observed_outcomes` / `flake_evidence` on a
  `VerificationResult` as ADDITIVE INSTANCE ATTRIBUTES -
  `shared/types.py` is another owner's file and was NOT edited, so no
  dataclass field changed; `evidence_of(result)` returns `None` for a result
  that never went through it, which reports an ABSENT receipt as absent
  instead of as stability.
  (8) **MEASURED cost (real Docker lane, `execution.sandbox.execute_sandboxed`,
  3 samples each, warm image, trivial 2-test fixture):** median wall for the
  target-run series alone is **3.36s at 1 repetition, 6.41s at 2, 7.59s at 3**
  (per-repetition 3.36s / 3.20s / 2.53s). So the default 1 -> 2 change adds
  **+3.05s median (+91% of the target-run phase)**, and the marginal
  repetition is cheaper than the first because the image and page cache are
  already warm. The per-task multiplier matters more than the absolute number:
  `harness/core.py` verifies the target after every step turn, so a
  15-turn task pays the extra run up to 15 times. Recommended split: 2
  repetitions at the two GATING post-fix sites (the final gate and the
  agent-tests post-fix, both of which can poison an attempt) and 1 at the
  NON-GATING per-step checkpoint, whose `flaky` value gates nothing and whose
  verdict then honestly reads `not_run`. Exact per-repetition cost scales with
  the target test's own runtime, not with this fixture.
  (9) **NOT WIRED - and this is the honest state.** `execution/verify.py`,
  `harness/core.py` and `harness/agent_loop.py` were owned by another terminal
  in the same parallel group, so the three call sites are a filed handoff
  (`execution/AGENTS.md`, "R2-02 cross-terminal requests") and not an applied
  edit. **Until they land, the default configuration still cannot fire this
  gate.** Nothing was weakened to compensate: the pre-existing
  `verify(..., rerun_for_flake_check=...)` signature, its 0/1-means-one-run
  behaviour, and the `flaky` boolean are all byte-identical.
  (10) Evidence: `python -m pytest tests/test_ceiling_r2_02_flake.py -q
  -p no:randomly` -> **41 passed**, including a genuinely alternating pytest
  fixture run through real `python -m pytest` processes that reaches
  `flaky_detected` on the DEFAULT repetition count, a stable fixture that
  asserts `repetitions >= 2` (so a vacuous pass is impossible), the
  same-fixture one-repetition control that reads `not_run`, and a real sleep
  exceeding a real timeout budget for the third outcome.
  `python -m pytest tests/test_verify.py -q -p no:randomly` -> **28 passed**
  (real Docker daemon). `python -m pytest tests/test_stubs_and_deps.py -q
  -p no:randomly` -> **10 passed**. `python -m evals.run --check` ->
  **14/14 CLEAN**. Ruff check/format clean on both new files. No live-provider
  lane was run and none is claimed.
- 2026-09-26 (Ceiling Terminal 16 - headless agent, ACP/serve, install and
  release truth): **new CLI surfaces and two ACP behavior changes. No Boundary
  0-5 signature, event kind, or serialized field was REMOVED or RENAMED; the
  `Task`/`TaskResult`/`ExecutionResult`/`VerificationResult`/`RunResult`
  shapes are byte-identical and the verifier still mints verified success.**
  (1) NEW `cli/headless.py` - the one-shot agent surface (`neo -p "sentence"`,
  `neo -`). `result_envelope` is the ONLY producer of the headless document
  and is built from `cli.runview.read_live_projection` +
  `effective_terminal_status` + `verification_state`, i.e. the SAME journal
  authority the TUI renders. `HEADLESS_MODES` declares the headless policy
  for `/plan`, `/review`, `/ask` (all `read_only=True`, `verifies=False`); a
  read-only mode reports `verification_state: not_run` and `verified: false`
  even when it exits 0. `completed_unverified` maps to exit 1 for every
  verifying mode. Session ids are `cli.session` conversation ids, so a
  headless turn and a TUI session share one history.
  (2) NEW `cli/serve.py` - `neo serve` (loopback `agent_sdk.server.AgentServer`)
  and `neo acp` (`acp.server.ACPServer` over a NEW server-side stdio
  transport bound to a real `agent_sdk.Agent`). A non-loopback bind requires
  BOTH `--allow-non-loopback` and a token; a missing model is a refusal with
  exit 4, not a server that fails every request. Protocol frames are the only
  thing on `neo acp`'s stdout.
  (3) NEW `cli/capability.py` - `neo capabilities`, the runtime-registry
  capability probe, the once-per-install "public release is older than these
  docs" notice, and the registry that `neo --help`'s `{...}` metavar is now
  DERIVED from (`cli.main.build_parser` writes it; `command_inventory` cross-
  checks it against the live parser and reports `drift`).
  (4) `acp/server.py` - two BEHAVIOR changes found by driving a real
  `agent_sdk.Agent`:
  (a) `_consume_stream` terminates a synchronous stream with
  `next(it, _ITERATOR_DONE)` instead of catching `StopIteration`, which
  asyncio converts to a `TypeError` that escaped the loop and hung every
  real-agent `session/prompt`;
  (b) `_invoke_agent_method` no longer binds an ACP session id to a `run_id`
  parameter, and no longer injects `cwd` through `**kwargs`. Both produced
  hard failures against a real agent (`event journal does not exist` and
  `RunRequest.__init__() got an unexpected keyword argument 'cwd'`) that a
  string-returning stub could not surface. `_result_model` additionally
  projects its terminal value through the new `_json_safe_public`, because a
  real `agent_sdk.Event` is not JSON-serializable and killed `json.dumps`.
  The 29-test ACP suite passes unmodified; no pin was weakened.
  (5) NEW `scripts/docs_truth.py` and `scripts/release_evidence.py` - the
  documentation/site truth gate and the single release-evidence report. A
  skipped lane is never a pass, and publishing requires an explicit human
  approval phrase; neither script can upload anything.
  (6) `cli/commands.py` was NOT changed. `/plan`, `/review`, `/ask` remain
  `refuse` in the bare `neo run` surface; the explicit headless policy lives
  in the surface that has one.
  Verification: NEW `tests/test_ceiling16_surfaces.py` 53 passed; CLI +
  ACP + SDK/server release selection 245 passed, 1 skipped; installers /
  release-workflows / dashboard / docs-lint / parity selection 111 passed,
  2 skipped, 1 failed (the pre-existing `mcp_server` `provenance` defect named
  by Ceiling-03 as Terminal 04's in-flight work - NOT a pass). No Docker lane
  and no live-provider lane was run, and neither is claimed.
- 2026-09-26 (Ceiling Terminal 07 - recovery, steering, doom-loop prevention):
  **`harness.tool_errors` becomes a recovery POLICY rather than a classifier;
  `BashSession` gains an additive optional cancellation token; the fix and
  legacy-agent loops gain three behavioural pre-dispatch checks and one
  in-flight abort watcher. No Boundary 0-5 signature, event kind, or
  serialized field was REMOVED or RENAMED. `state.json` is unchanged
  (Boundary 4's six-key prefix is byte-identical).**
  (1) `harness/tool_errors.py` - NEW additive surface. Everything below is
  new; `ToolError`, `classify`, `classify_exception`, `render_error` and
  `error_kind_of` keep their exact signatures and behaviour.
  Output shaping: `OMISSION_MARKER` (the single explicit
  `[... N chars omitted ...]` marker), `OMISSION_RE`, `is_pytest_shaped`
  (samples head AND tail - a long pytest run opens with a collection log
  containing no marker, so a head-only check misclassifies the very output
  this exists for), `shape_tool_output(text, limit, *, head_ratio=None)`
  (head+tail with the marker always present; `PYTEST_HEAD_RATIO`=0.15 bias for
  pytest-shaped output so the failure tail survives). `harness.tools.truncate`
  is UNCHANGED (test-pinned); `BashSession` now shapes through
  `shape_tool_output`.
  Command narrowing: `narrower_command(command, *, max_depth=2) -> str|None`
  - pytest family gets `-x` and/or `--tb=short`; `find` gets `-maxdepth`;
  `grep`/`rg` get a single-file-type filter. Returns None rather than
  inventing a rewrite.
  Model failure tolerance: `ModelFailure` (kind / detail / retryable /
  status_code / backoff_s / terminal), `classify_model_failure(exc, ...)`,
  `backoff_s(attempt, *, base_s, cap_s, multiplier)`, `ModelRecovery` with
  `call(fn, *, step)` / `report()`. Provider classes are
  `model_rate_limited` (429), `model_unavailable` (5xx/connection/overload),
  `model_timeout`, `model_auth` (401/403 - TERMINAL), `model_bad_request`
  (400/404 - TERMINAL), `model_internal` (a HARNESS exception - TERMINAL and
  never retried). An unrecognised PROVIDER failure defaults to
  `model_unavailable`/retryable; a non-provider exception is always
  `model_internal` regardless of how provider-flavoured its message is.
  `BashSession` is unaffected. Every decision emits a `model_recovery` event
  `{label, step, attempt, max_attempts, kind, detail, status_code, retryable,
  terminal, backoff_s, action}` where `action` is `retry` or `give_up`.
  Per-kind recovery policy: `POLICY` is the kind -> (action slug, instruction)
  table - every kind maps to a DISTINCT action, and that distinctness is
  test-pinned (`test_every_policy_kind_has_a_distinct_action`). `RecoveryAction`
  (kind / action / instruction / evidence / next_command / timeout_s /
  forbidden / stop / replan / attempts), `RecoveryPolicy` (one per task:
  `set_turn`, `on_tool_error`, `on_malformed_tool_call`,
  `on_verification_failure`, `note_command`, `release_blocked_command`,
  `forbid`, `rejects`, `next_timeout_s`, `directory_listing`,
  `numbered_file`, `is_read_only_command`, `stats`, `feedback`),
  `recovery_policy_from_config(cfg, *, root=None)`, `command_paths()`,
  `render_schema_recovery()`.
  Kind -> action: `timeout` -> `narrow_and_extend` (a strictly narrower next
  command AND a larger bounded budget); `file_not_found` ->
  `attach_listing` (a real bounded directory listing);
  `permission_denied` -> `forbid_path` (the path is added to a do-not-retry
  set and a command whose EVERY path token is forbidden is refused BEFORE it
  runs); `malformed_patch` -> `attach_numbered_file` (the exact current file
  with line numbers; the patch file itself is never presented as the target);
  `malformed_tool_call` -> `restate_schema` (schema + ONE worked example);
  `model_unavailable` -> `bounded_fallback`; `verification_failed` ->
  `preserve_and_replan` (the verifier evidence is preserved verbatim, bounded,
  and `replan=True`); `loop_detected` -> `stop_and_ask`; plus
  `command_not_found` -> `avoid_binary`, `command_rejected` -> `avoid_shape`,
  `internal_error` -> `inspect_output`.
  `RecoveryPolicy.stats()` reports per-kind occurrences, recoveries, a pending
  flag and `mean_turns_to_recovery`; an error still pending at report time is
  NOT counted as recovered, so an unrecovered failure can never read as a fast
  recovery.
  (2) `harness/steering.py` - NEW `HardAbortWatcher(buffer, on_abort, *,
  poll_interval_s=0.05)` with `start()` / `stop(join_timeout_s=5.0)` /
  `aborted` / `abort_seen_at` / `abort_texts` / `abort_reason` / `errors` /
  `elapsed_to_abort_s()`, plus module constants `ABORT_POLL_INTERVAL_S` and
  `ABORT_JOIN_TIMEOUT_S`. It polls the SAME journal for an `abort` intent
  while a command is in flight and calls a ZERO-ARGUMENT kill hook. It never
  consumes the event (the loop's single consume point still owns it) and it
  records a failing hook in `errors` rather than hiding it.
  (3) `harness/tools.py` - `BashSession.__init__` gains a 4th OPTIONAL
  `cancellation_token=None`; `cancel()`, the `cancelled` property, and
  `set_timeout(timeout_s)` (which can only WIDEN the budget) are new. The
  token is forwarded to the sandbox ONLY when the resolved callable's
  signature accepts `cancellation_token`/`cancel_event`/`**kwargs`, so every
  three-positional-arg Boundary-1 sandbox and test double keeps working
  byte-identically. `BashSession.last_error` now holds the structured
  `ToolError` of the most recent failing command (None on success), so a
  loop can select a policy from a KIND instead of re-parsing rendered prose.
  (4) `harness/core.py` + `harness/agent_loop.py` - `run_step` gains two
  optional keyword params (`policy`, `model_recovery`); a direct caller that
  omits them gets fresh instances, so there is one code path. The model call
  is now `ModelRecovery.call(...)`: a retryable provider failure is retried
  with bounded backoff and a TERMINAL one alone reaches `FATAL:` (now
  `FATAL: terminal model failure (<kind>) ... after N attempt(s)`), with a
  `model_failure_terminal` event. Before a command runs, three checks can
  change what happens: a pending hard abort (yield, `steering_step_yield` at
  `pre-dispatch`), a forbidden-path refusal (`command_refused`), and the
  repeated-command guard (`loop_guard`, step note `LOOP-GUARD: ...`). While a
  command runs, a `HardAbortWatcher` is armed against the session token; an
  observed abort yields `steering_abort_in_flight` with
  `{command, elapsed_s, result_preserved_chars, watcher_joined, texts}`, the
  command's output is appended to the conversation BEFORE the step ends, and
  the note says the result was preserved. Watcher failures emit
  `steering_abort_watcher_error`. A failed command emits `command_recovery`
  with the full `RecoveryAction`. Loop exits still write `work/`,
  `state.json` and `plan.json`, so an aborted run stays resumable. A
  `recovery_stats` event `{policy, model}` is emitted before `task_end` on
  both the success and failure paths.
  (5) `harness/config.py` - additive keys, all defaulted:
  `recovery_timeout_backoff` (1.5), `recovery_max_timeout_s` (600),
  `recovery_max_repeat_command` (2), `recovery_loop_guard_read_only`
  (False), `recovery_listing_limit` (40), `recovery_numbered_file_lines`
  (160), `recovery_numbered_file_chars` (6000),
  `recovery_fallback_model` (None), `max_model_attempts` (3),
  `model_retry_base_s` (0.5), `model_retry_cap_s` (8.0),
  `abort_kill_deadline_s` (5). `recovery_loop_guard_read_only` defaults False
  so repeated READ-ONLY commands are exempt, matching the typed tool
  catalog's own `loop_guard_read_only`; a repeated BUILDING command is never
  exempt. This exemption is load-bearing: without it the guard converts
  legitimate "re-read after your own edit" sessions into failures, which the
  real Docker e2e lane caught.
  (6) NEW trace events (all additive, safe to surface in dashboards and in
  the TUI's journal projection): `model_recovery`,
  `model_failure_terminal`, `command_recovery`, `command_refused`,
  `loop_guard`, `recovery_action`, `recovery_stats`,
  `steering_abort_in_flight`, `steering_abort_watcher_error`. The existing
  `tool_error` event gained additive `action` and `recovery` keys.
  (7) NOT CHANGED, deliberately: no verifier, no `CompletionPolicy`, no
  completion status, no `state.json` key, no Boundary 1/2/3 signature. The
  verifier still mints verified success; the success mint is still keyed on
  `final_v.target_test_passed and final_v.regression_passed and not
  final_v.flaky` and the recovery machinery appears nowhere in that condition
  (`test_the_verifier_contract_is_untouched_by_recovery` pins this).
- 2026-09-26 (Ceiling Terminal 03 - global sessions, search, fork, safe
  artifact storage): **one changed default for `memory.paths`, one new
  read-model subsystem in `cli/session.py`, three additive slash commands,
  one new top-level flag. No Boundary 0-5 signature, event kind, or
  serialized field changed.**
  (1) `memory/paths.py` - LOCATION CONTRACT CHANGED (this is the one
  behavioral break, and it is the point of the round).
  `default_logs_dir()` no longer returns `<cwd>/logs`; with no
  `HARNESS_LOGS_DIR` it now returns `<neo_home>/logs/<repo-key>`, i.e. a
  harness-owned home OUTSIDE the user's repository. `HARNESS_LOGS_DIR`
  still wins. `harness_home()` follows the same rule (its default was
  `<cwd>/.harness`; `NEO_HOME` then `HARNESS_HOME` still override), which
  also moves `decisions_db_path()` and the code-graph index out of the
  repository. NEW: `neo_home()`, `repo_key(repo)` (stable
  `<slug>-<10 hex sha256 of the canonical path>`), `canonical_repo()`,
  `find_git_root()`, `is_within()`, `log_root_placement(log_root, repo)`,
  `ensure_log_root_ignored(log_root, repo)`, `artifact_root_warnings()`.
  A FORCED in-repo root (a `--log-root` flag or a configured `log_root`)
  is appended to that repository's `.gitignore` (idempotently, only inside
  a real repo) and reported through `warning`; it is never silent and never
  deletes. `_gitignore_covers` is deliberately duplicated from
  `cli.neoconfig` rather than imported: the dependency direction is
  cli -> memory, so memory may not import cli.
  (2) `cli/session.py` - NEW global per-repo session index (a lookup
  accelerator, NOT a status authority; it never opens a `trace.jsonl`).
  `index_session(record)`, `index_conversation(session, log_root, ...)`,
  `index_run(log_root, task_id, ...)`, `index_records(*, repo=, query=,
  limit=, verify=, log_root=)`, `cross_repo_sessions(query, repo, limit)`,
  `index_compact(max_records)`, `index_stats()`, `index_roots()`,
  `index_newest_resumable(*, repo, verify, scan, log_root)`, `index_remove(id)`,
  `index_dir()`. Storage is `session-index/index.jsonl` (append-only
  journal of array rows) plus `session-index/index.json` (compacted
  snapshot the read path prefers; a listing reads the journal only as a
  tail from the byte offset the snapshot recorded). Each row stores repo
  path, repo key, repo name, branch, worktree, task id, status, resumable,
  issue preview, updated_at, turn count, artifact root, and fork lineage.
  Hooked into `load_or_create`, `save_session`, `fork_session`,
  `import_session`, and `recover_session`; `cli.interactive.record_session`
  additionally upserts the run row. A partial trailing journal line is
  ignored, compaction is snapshot-then-truncate, and the read merge is
  idempotent by id, so an interrupted compaction re-reads rather than loses.
  (3) `cli/session.py` - lifecycle helpers surfaced to both shells:
  `resolve_artifact_root(explicit, repo, file_config)` (the ONE log-root
  authority; returns `log_root`/`source`/`placement`/`warnings`),
  `startup_recovery_candidate(log_root, repo)` (the newest UNREADABLE
  conversation, or None), `recover_corrupt_session(..., strategy=)`,
  `resolve_session_token(log_root, token, repo)` and
  `resolve_index_session(token)` (exact-or-unique-prefix session ids;
  a prefix under 4 characters is refused, an ambiguous prefix is reported,
  never guessed). Existing `fork_session` / `export_session` /
  `import_session` / `recover_session` signatures are UNCHANGED; the
  recovery helpers wrap them and re-index the result. Recovery QUARANTINES
  (`<name>.corrupt-<ms>`) and never deletes.
  (4) `cli/commands.py` - three NEW `CommandSpec` entries (`/fork`,
  `/import`, `/recover`, 40 -> 43) plus their `HEADLESS_COMMAND_POLICIES`
  rows (`mapped`). `REQUIRED_COMMANDS` is unchanged: these are additions,
  not restatements of the required 33. `HEADLESS_COMMAND_POLICIES` also
  gained rows for `/doctor`, `/open`, and `/repo`, which landed in
  `COMMAND_SPECS` from a parallel terminal while this round was in flight
  and left the import-time table validation failing; those three entries
  are this terminal's repair, not its feature.
  (5) `cli/main.py` - NEW top-level flag `--repo-filter REPO` for
  `--list-sessions` / `--continue` (read pre-parse from argv, because those
  flags dispatch before argparse). NEW private helpers
  `_artifact_log_root(explicit, repo)` and `_repo_filter_from_argv(raw)`.
  `cmd_fix`, `cmd_run_benchmark`, and `_run_finding` now resolve their log
  root through `_artifact_log_root`; every other call site already used
  `default_logs_dir()` and therefore inherits the safe default.
  (6) `cli/interactive.py` / `cli/tui.py` - `/sessions` now lists ACROSS
  repositories by default (from any CWD) with an explicit `repo:` filter;
  an EXPLICITLY chosen artifact root stays an isolation boundary
  (`_headless_scoped` for the headless surface, the new
  `NeoApp(root_scoped=...)` flag for the TUI). `most_recent_resumable`
  takes `cross_root` (default False) and `/resume` resolves a conversation
  short id across roots. NEW handlers `_fork_command`, `_import_command`,
  `_recover_command`, `_resume_conversation_command`,
  `_startup_recovery_offer`, `_resolve_session_artifact_root`,
  `normalize_index_row`, `_index_matches`, `_session_arg`; the TUI mirrors
  the three lifecycle commands and prompts for startup recovery through
  `_ConfirmScreen`. `run_interactive` / `run_tui` no longer default to
  `Path("logs")`. The `--continue` verification rule: the index picks the
  candidate, then that ONE run is re-verified against its own journal
  (`_index_verify`), so a stale "resumable" row can never resume a finished
  run while listing still costs no trace reads.
- 2026-09-26 (Ceiling Terminal 08 — verification intelligence and
  machine-checkable specs): **five NEW `execution/` modules and three
  ADDITIVE keyword-only parameters on Boundary 1. No existing signature,
  return type, or serialized field changed.**
  (1) NEW `execution/spec_ledger.py` — a JSON feature/spec artifact with
  immutable items. `SpecItem(id, title, acceptance, tests, passes)`; the
  agent may flip `passes` and nothing else. Immutability is mechanical:
  `SpecLedger.write_seal()` writes a SEPARATE `spec.seal.json` holding a
  SHA-256 over the obligation projection (`id`/`title`/`acceptance`/`tests`
  — `passes` is deliberately excluded) plus per-item projections;
  `SpecLedger.guard_against(seal)` returns a `SpecGuardReport` whose
  `removed` / `added` / `mutated` name the offending items. An UNSEALED spec
  reports `ok=False, sealed=False` — never a pass. `load_or_report(root)`
  reports instead of raising so "there is no spec" is a recordable gate
  failure. `SpecLedger.apply_claims()` RAISES `SpecViolation` for an unknown
  id rather than dropping it.
  (2) NEW `execution/test_selection.py` — stdlib-`ast` import-graph test
  selection for the inner loop. `build_import_graph` (no index, no
  tree-sitter, no network) → `select_tests(repo, changed_files)` →
  `TestSelection(files, node_ids, strategy, total_tests, missing,
  coverage_fraction)`. Soundness over tightness: a test is selected when it
  imports the changed module directly or transitively, a changed file that IS
  a test selects itself, an unresolvable graph falls back to ALL tests, and
  `strategy` is `bounded` when `max_files` clips the set.
  `save_selection` / `load_selection` / `default_selection_path` persist the
  choice as `test_selection.json` beside the run. An empty selection
  (`is_empty`) is a "cannot vouch" signal, never a pass.
  (3) NEW `execution/result_parsing.py` — report-first verdicts.
  `parse_test_run(result, junit_xml=..., json_report=..., expected_tests=...)`
  → `TestRunReport(outcome, exit_code, tests_collected/passed/failed/skipped,
  source, confidence, notes)`. Evidence order is machine-readable report →
  exit code → prose, and `source` records which one won. `outcome` is one of
  `pass` / `fail` / `timeout` / `no_tests` / `error`; a TIMEOUT is always its
  own outcome (detected before anything else, including an exit 0), a
  zero-test run is `no_tests` (exit 5, a `0 passed`/`0 tests` count, a
  no-tests marker, an empty capture at exit 0, or `expected_tests>=1` with
  none collected), and a crash-shaped capture (traceback / `INTERNALERROR` /
  unhandled `OSError`) with NO test counts is `error`, not "1 failed".
  `parse_junit_xml` and `parse_pytest_json` degrade to `None` on an
  unrecognized shape so the caller falls back instead of inventing counts.
  (4) NEW `execution/flake.py` — clean-environment failure confirmation.
  `assess_failure(report, run=..., repo_path=..., attempts=...)` →
  `FlakeAssessment(classification ∈ confirmed_regression | pre_existing_flake
  | transient_failure | unconfirmed, actionable, edit_instruction, ...)`.
  Only `confirmed_regression` is actionable; an UNCONFIRMED failure produces
  NO edit instruction, which is the documented policy rather than a missing
  feature (`attempts=0` is the explicit do-not-confirm arm).
  `clean_environment(repo)` copies the tree to a staging directory and purges
  `__pycache__`, `.pytest_cache`, `.coverage`, order-dependence markers, and
  friends so a rerun cannot agree with the failure for the wrong reason;
  `scrub_run_environment()` drops `PYTEST_*` / `NEO_*` / `HARNESS_*` and any
  inherited `PYTHONPATH`. `FlakeLedger.confirmation_rate` is `decided/total`
  (an all-unconfirmed run reports 0, not 100) and `confirmed_rate` is
  `confirmed/decided`. `run_local_command` is an explicitly HOST-side lane for
  measurement; it is not wired into `verify()`.
  (5) NEW `execution/independent_evidence.py` — held-out acceptance plus a
  separate judge. `build_held_out_suite(root, cases, seed=...)` materializes
  acceptance tests OUTSIDE the repository under test (enforced, raises
  `ValueError` otherwise), with a digest-protected `conftest.py` and
  `pytest.ini`, and CONCEALS them (POSIX mode 0, plus a hard `PermissionError`
  from `HeldOutSuite.read`) while the build loop runs. Randomization is seeded
  ORDER randomization, not value synthesis, so a correct implementation always
  passes. `detect_tampering(suite, expected_fingerprint=...)` compares
  per-file digests and reports changed/missing/added files; an absent
  pre-loop fingerprint is reported as a FAILURE, not as intact.
  `judge(suite, run=..., visible_report=..., expected_fingerprint=...,
  threshold_points=5.0)` → `Judgment(verdict, visible_score, heldout_score,
  gap_points, lucky_pass, tampering, reasons)`, where `gap_points =
  visible_pct - heldout_pct` is the reward-hacking gap and a gap above the
  threshold is a reported lucky pass. `premature_completion(...)` is the
  mechanical "claimed complete without a clean final gate" check.
  (6) NEW `execution/verification_intelligence.py` — the gate.
  `run_verification(...)` composes all five in order (spec guard → incremental
  inner run → FULL-suite final gate → clean-environment flake confirmation →
  independent judge) and returns a `VerificationOutcome` carrying
  `GateResult` rows, every `TestRunReport`, the selection, the spec guard, the
  flake assessment, the judgment, timings, and errors. The MINT RULE is
  structural: `mint_success()` is true only when the final gate RAN, the
  regression scope was `full`, and every MANDATORY gate passed (a skipped
  mandatory gate counts as a failure); `final_result()` RAISES when the final
  gate did not run, so a caller cannot read a subset result as a verdict.
  `final_gate` failing (e.g. `SandboxUnavailableError`) yields
  `status="indeterminate"`, never a pass.
  (7) Boundary 1 `execution.verify.verify(...)` gains three ADDITIVE
  keyword-only parameters, defaulted so every existing call site is
  byte-compatible: `selection=None` (an import-graph `TestSelection`),
  `final_gate=True` (the only gating path; when True the regression run is the
  FULL suite and a supplied selection is IGNORED for gating), and
  `reports=None` (an out-list every run appends its `TestRunReport` dict to, so
  a trace records HOW each verdict was reached). `verify` still returns
  `shared.types.VerificationResult` unchanged; `shared/types.py` was NOT
  edited. NEW `inner_verify(repo, changed_files, ...) -> (VerificationResult,
  TestSelection)` is the explicitly NON-GATING repair-loop entry point and
  falls back to the full suite when the selection is empty. `harness/_stubs/
  verify.py` was NOT changed and stays signature-compatible with the default
  call shape.
  (8) `evals/daily_driver.py` gained the `verification_intelligence` feature
  evidence probe (baseline = spec guard + selection + held-out judge; the
  `adversarial` arm runs the same real gate with the intelligence OFF and NO
  spec present, so the refusal is attributable to the gate rather than to the
  fixture) plus its `ActivePromptFeature` entry, and
  `_run_feature_lane`'s default wall-clock budget moved 240s → 900s because
  that probe drives the real Docker verifier for both arms. The registry is
  now 14 features × 2 arms.
  (9) `harness/_stubs/verify.py`, `shared/types.py`, `harness/core.py`,
  `harness/agent_kernel/completion.py`, and `harness/agent_loop.py` were NOT
  edited. The kernel's existing `completed_verified` mint rule is unchanged;
  this round supplies it with harder-to-fool evidence rather than a second
  authority.
  Verified: `python -m pytest tests/test_ceiling08_verification.py -q` →
  78 passed (74 host + 4 real Docker). `python -m pytest tests/test_verify.py
  tests/test_verify_js.py tests/test_feedback.py -q` → 93 passed against the
  real Docker daemon. `python -m pytest tests/test_daily_driver_evals.py
  tests/test_evals_run.py tests/test_evals_tasks.py -q` → 69 passed.
  `python -m evals.run --suite prompt-regression --check` → 14/14 CLEAN.
  No live-provider lane was run and no credential was inspected.
  Known pre-existing failure in this shared tree, NOT caused by this round:
  `tests/test_e2e_run_task.py::test_verified_success_state_complete_after_exhausted_turns`
  asserts `"exhausted"` in a step note, but Terminal 04's `ToolLoopGuard`
  (`harness/agent_kernel/tools.py:746`) stops the step first with
  `LOOP-GUARD: repeated identical command (3x)`. That file is untracked and
  outside this terminal's ownership.
- 2026-09-26 (Ceiling Terminal 12 — hooks, skills, automation, MCP namespace):
  **four new additive surfaces, no existing signature changed.**
  (1) NEW `extensions/user_hooks.py` — the DECLARATIVE lifecycle-hook layer
  (distinct from the in-process `extensions/hooks.py` plugin contract). Seven
  events: `SessionStart`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
  `Stop`, `PreCompact`, `SessionEnd`. Three config tiers merge with explicit
  precedence `local` > `project` > `user`
  (`<global config root>/hooks.json`, `<repo>/.neo/hooks.json`,
  `<repo>/.neo/hooks.local.json`); a colliding hook `id` is REPLACED by the
  higher tier, and dispatch order is `(tier_rank, declared_order, id)`. A
  `matcher` and an `if` block are AND-combined and use the SAME six
  permission-rule dimensions as `harness.agent_kernel.policy.PolicyRule`
  (`tool`, `path`, `command_prefix`, `mcp_server`, `network_domain`,
  `side_effect_class`); an unknown dimension name is a typed error. Handler
  types: `command` (fixed argv, `shell=False`, scrubbed env, repo-pinned cwd,
  per-hook timeout, 8 KiB stdout cap; a shell STRING is refused),
  `http` (one bounded POST through `shared.egress`, so an unlisted host never
  opens a socket), `prompt` (OPTIONAL, additive only, and a skip without an
  injected renderer is recorded). Hook output is bounded and typed to
  `decision`/`reason`/`systemMessage`/`additionalContext`/`suppressOutput`;
  policy-shaped keys (`permission`, `policy`, `approval`, `allowed_tools`,
  `tools`, `env`, `config`, `sandbox`, `egress`, `model`, `provider`) are
  dropped and NAMED in `HookDecision.refused_policy_keys`. Observational
  events may add context and suppress output but never decide (a block is
  rewritten to `continue` and the row records
  `suppressed_for_observational=True`); blocking events use restrictive
  precedence, strictly-greater, so equal-rank decisions are first-wins.
  Latency is per-hook and per-dispatch, with `max_latency_s` spent across a
  dispatch and the remainder recorded as `latency_budget_exhausted`. Failure
  is never silent: timeouts (124), missing executables (127), spawn failures
  (126), unusable output, and refused egress all land in
  `HookOutcome.failures` and mirror to `shared.tracing.emit("extensions",
  "user_hook", ...)`. Verifier integration: `PostEditGate` returns a typed
  `PostEditGateReport` for the declared target-test/lint subset, and
  `CompletionGate.evaluate(...)` makes a blocking `Stop` hook turn
  `completed_verified` into `completed_unverified`; the gate ONLY downgrades
  and additionally refuses a green hook over incomplete verifier evidence.
  (2) NEW `extensions/skill_policy.py` — versioned skill/agent declarations
  (`version`, `model_tier`, `tools`, `permissions`) with
  `SessionPermissions` as the parent-session envelope. `resolve_permissions`,
  `resolve_tools`, and `clamp_tier` are monotonic: a claim the parent does not
  already permit is REFUSED AND NAMED, never granted, and no configuration
  value turns a refusal into a grant. `explain_selection` / `explain_agent`
  produce the trace record (considered, selected, matched terms, effective
  tier, intersected permissions, every refused claim); an empty selection is a
  first-class record. `load_agent_specs(root)` loads `agents/*.md` from an
  explicitly supplied root and refuses symlinks/oversize/unsafe names with a
  recorded diagnostic. `declaration_from_object` accepts a
  `harness.skills.Skill` OR a `runtime.subagents.AgentDefinition` (Terminal
  06) so there is ONE permission-intersection implementation, not one per
  loader — the role-profile ceiling and the parent-session ceiling compose.
  (3) `harness/skills.py` — ADDITIVE: `Skill` gains keyword-defaulted
  `version`, `model_tier`, `permission_claims`, `declared_tools`; the
  frontmatter parser now also accepts a YAML block sequence under
  `permissions:`/`tools:`; each matched-skill receipt gains a `declaration`
  object and `build_skill_receipt` gains a `declarations` summary. The four
  fields are INERT DATA — enforcement is only in `skill_policy`. No existing
  `Skill` field, receipt key, or caller changed.
  (4) NEW `runtime/schedules.py` — `neo run --at` / scheduled issue queues as
  bounded, logged, revertible REQUESTS. `logs/_schedules/` holds one atomic
  record per schedule plus `events.jsonl`; `revert()` moves the record to
  `reverted/` with `reverted_at` and `revert_reason`. `execution_policy(schedule,
  inherited=...)` is the only request-to-`Task.config` conversion and it
  returns `(config, clamped)`: `approval` is MONOTONE
  (`{auto:0, off:0, never:0, require:1}` — a schedule may demand `require` and
  may never weaken an inherited `require`), every budget key is clamped to the
  minimum of request and inherited ceiling, an unknown `agent_strategy` is
  dropped rather than guessed, credentials in the config or issue text are
  refused at registration, and `automation`/`automation_schedule_id` are
  stamped last and are not overridable. `complete()` stores the run's own
  canonical status verbatim — the registry has no vocabulary for upgrading
  `completed_unverified`. `parse_at` accepts ISO-8601, an epoch, or
  `+15m`/`+2h`/`+1d` and REFUSES a past value. CLI: `neo run --at <when>
  "<command>"` (plus `--schedule-id`, `--schedule-every`, `--schedule-max-runs`)
  and `python -m runtime.schedules {register,list,claim,complete,revert}`.
  (5) NEW `mcp_server/namespace.py` — MCP namespacing, least privilege, and
  approval-time hash pinning. Tool ids are `mcp__<server>__<tool>` (the
  convention common clients already parse); segments are normalized so a
  hostile label can never inject the separator, and
  `parse_namespaced_tool_name` round-trips exactly or refuses with no
  best-effort parse. `MCPToolPolicy` is per-server AND per-tool, closed by
  default (unlisted server refused, unlisted tool refused, a tool declaring no
  side-effect class treated as mutating) with an ordered
  `read < search < network < mutation < destructive` ceiling;
  `visible()` makes the rendered catalog the callable catalog.
  `tool_definition_digest` is a domain-separated (`neo/mcp-tool/v1`) SHA-256
  over canonical `(server, tool, description, inputSchema)`; `ToolPinSet`
  records the digests an approval was granted against and `verify()` re-hashes
  the live catalog immediately before the call, raising
  `MCPToolDefinitionChanged` on a mismatch or a vanished tool. Least privilege
  is checked BEFORE the pin so an unauthorized tool is refused as unauthorized.
  `review_tool_result` routes an MCP result through
  `shared.security.review_untrusted_source(source="mcp")` and forwards only
  reviewed text. `mcp_server/server.py` gains additive, non-tool projections
  `server_name()`, `server_tool_definitions()`, `server_tool_catalog(policy)`,
  `server_tool_pins()`, and a `_SERVER_TOOL_SIDE_EFFECTS` map whose default is
  `mutation` — no tool added, removed, or re-signed, and `server_health()`'s
  six-key projection is byte-identical.
  **Not wired, by design and recorded as a handoff:** nothing in
  `harness/agent_kernel/kernel.py` or the legacy loops calls `HookEngine` yet
  (the public per-event API and the exact integration point are in
  `logs/ceiling/terminal-12.json`); no consumer executes a due schedule; and
  `skill_policy` is not yet called from the planning path. `extensions/`
  imports neither `cli/` nor `harness/`, so the dependency direction stays
  clean. No Docker and no live-provider lane was required or run for this
  round's own verification; `tests/test_ceiling12_hooks.py` is 36 passed with
  1 Windows symlink-privilege skip.
- 2026-09-26 (Ceiling Terminal 06 — planning, subagents, worktrees, and
  orchestration): **the `task` tool becomes a real bounded spawn into the
  existing runtime DAG; planning becomes machine-readable plan state with a
  replan policy; claims become AST-symbol scoped with leases; merges are
  serialized and verified; `neo worktree new|list|go|rm` and
  `neo fix --worktree NAME` exist.**
  (1) NEW `runtime/planning.py` owns bounded plan state: `PlanStep`
  (mandatory `behavior`, `files` mandatory for `edit` steps, optional
  `symbols`/`depends_on`), `PlanState` (versioned, `2..max_steps` steps,
  `max_steps` clamped into `[PLAN_STEP_FLOOR=2, PLAN_STEP_CEILING=12]`),
  `build_plan`, `plan_from_model_steps`, `assess_step_outcome`,
  `record_step_outcome`, `apply_replan`, `save_plan_state`/`load_plan_state`,
  `render_plan_block`, and `plan_digest`. The replan reasons are the stable
  slugs `constraint_violation`, `turns_exhausted`, `no_files_touched`; a
  replan supersedes one step, inserts a re-scoped replacement, bumps
  `version`, and appends a machine-readable `replans` record. Persisted state
  is `{"schema_version": 1, ...}`; an unknown schema version fails closed.
  New config keys `max_plan_steps` (12) and `plan_step_turn_budget` (12).
  (2) NEW `runtime/subagents.py` owns subagent definitions and admission:
  `AgentDefinition` (versioned Markdown frontmatter or TOML `[agent]` table
  with `name`/`role`/`model_tier`/`tools`/`permissions`/`max_children`/
  `max_turns`/`max_cost_usd`; a definition that requests a tool its role does
  not expose, requests the `task` tool, declares an unknown role, or declares
  a semantic major other than `AGENT_SCHEMA_VERSION=1` is REFUSED at load and
  reported through `AgentRegistry.diagnostics`), `AgentRegistry.load`,
  `SubagentLimits` (depth, children per parent, concurrency, per-run request
  cap, per-child/total cost, `summary_max_chars`), `SubagentRequest`,
  `SpawnRequestStore` (the durable cross-process queue), `SubagentAdmission`
  (a refusal is a VALUE with a reason, never an exception),
  `ChildSummary` (bounded render, default cap 2048 chars), and
  `SubagentSpawner` (resolve -> admit -> hand the node to
  `Orchestrator.spawn_child`). New config keys `subagent_max_depth` (2),
  `subagent_max_children_per_parent` (4), `subagent_max_concurrent` (4),
  `subagent_max_spawn_requests` (16), `subagent_max_child_turns` (12),
  `subagent_max_child_cost_usd` (2.0), `subagent_max_total_cost_usd` (10.0),
  `subagent_summary_max_chars` (2048), `subagent_default_agent` ("").
  (3) NEW `harness/agent_kernel/subagents.py` is the kernel side of the `task`
  tool: `SubagentToolRuntime.from_context` resolves either an in-process
  `SubagentSpawner` (the run owns the orchestrator) or the workflow's durable
  `SpawnRequestStore` (pinned on every child packet as
  `config["orchestration_spawn_dir"]`), and `dispatch_task_tool` returns a
  bounded model-facing receipt. With NEITHER bound the call is refused with
  `error_kind="no_runtime"` — it is never executed through an untyped
  fallback. `harness/agent_kernel/tools.py::_control_handler` routes `task`
  there; every call (admitted, queued, or refused) is still journaled as a
  `control_intent` event. The `task` catalog entry in
  `harness/tools.py` (the single canonical tool catalog) gained the OPTIONAL
  arguments `agent`, `files`, `symbols`, `depends_on`; `description` remains
  required and no existing argument changed. Role profiles in
  `runtime/roles.py` are unchanged, so no closed profile exposes a spawn tool
  by default.
  (4) NEW `runtime/symbols.py` provides AST symbol scopes and the
  unclaimed-symbol guard: `symbols_in_source`/`symbols_in_file` (stdlib `ast`
  for Python; a conservative top-level scan for JS/TS; empty for anything
  else, which forces a whole-file claim), `SymbolSpan`, `claim_resources`,
  `parse_patch_files`, `enclosing_symbol`, and `unclaimed_symbol_edits`.
  `ClaimStore` learned the `symbol:<path>::<name>` resource form (two symbol
  claims conflict only when identical; a whole-file claim covers the file), a
  lease (`heartbeat_epoch`/`pid`/`lease_s`), `live_owners`, `expired_owners`,
  and `reclaim_stale(live_owners=..., dead_owners=...)`.
  `WorkflowLimits` gained `max_children_per_parent` (4),
  `max_dynamic_nodes` (8), `claim_lease_s` (120), `symbol_claims` (True),
  `merge_enabled` (True), `merge_verify_timeout_s` (300), and
  `coordination_budget_fraction` (0.10).
  (5) `runtime/orchestration.py` grew the orchestration surface:
  `Orchestrator.spawn_child` (the single admission point for `task`, with
  node/depth/fanout re-checks and a durable `live_spec` so spawned children
  survive a restart), `active_children`/`active_child_count`,
  `coordination_report` (measured coordination seconds, p95, and share of
  wall clock against the declared budget), `merge_children` (one merge lock,
  dependency order, per-merge verification, aggregate parents reported rather
  than re-merged), `WorkflowSpec.topological_nodes`, and the events
  `child_spawn_admitted`, `subagent_spawn_refused`, `claims_reclaimed`,
  `merge_applied`, `merge_refused`, `merge_failed`, `merge_skipped`,
  `merge_completed`, and `dynamic_children_restored`. An injected
  `child_executor` that dies hard is now treated as a child crash instead of
  escaping as `SystemExit`. `runtime/worktrees.py` gained
  `ensure_integration()` (the single merge destination, `INTEGRATION_NODE_ID`),
  `git_check`, and `changed_files`.
  (6) CLI: `cli/commands.py` owns the `neo worktree` surface
  (`worktree_root`, `worktree_new`, `worktree_list`, `worktree_path`,
  `worktree_remove`, `worktree_run_config`, `cmd_worktree`, typed
  `WorktreeCommandError`); `cli/main.py` gained the additive `worktree`
  subcommand (`new|list|go|rm`, each with `--repo/--log-root/--json`) and the
  `fix --worktree NAME` flag, which materializes the isolated checkout BEFORE
  any model call and points `Task.repo_path` at it. A dirty source checkout
  fails closed (exit 2).
- 2026-09-26 (Ceiling Terminal 09 — prompt caching, cost, and latency):
  **Boundary 2's ledger rows and `get_last_usage()` gain prompt-cache and
  context-window receipts; `harness.prompts` gains a prefix-stable step
  renderer; `harness.retrieve_context` gains a content-digest cache and a
  `cache=` keyword; `execution` gains an explicit task-scoped warm sandbox;
  `/cost` and `neo fix --json` expose the cache hit rate.**
  (1) NEW `runtime/prompt_cache.py` owns the cache breakpoint and the
  receipts: `plan_cache` (pure), `cache_control_kwargs` /
  `apply_cache_parameters` (copies, never mutate the caller's list),
  `prefix_digest` / `tool_schema_digest`, `receipt_from_usage`, and the
  context-local `PromptCacheLedger` with `cache_hit_rate` and
  `cache_invalidation_count`. `runtime/model_router.py::call_model` gained
  NO new required parameters; it computes the plan, sends
  `cache_control: {"type": "ephemeral"}` to Anthropic-family endpoints only
  (OpenAI-compatible/Google endpoints cache a stable prefix implicitly and
  must not receive an unknown key), and writes `cache_status`, `cache_hit`,
  `cached_input_tokens`, `cache_creation_input_tokens`,
  `cache_prefix_sha256`, `cache_tool_schema_sha256`,
  `cache_breakpoint_index`, `cache_prefix_tokens_estimate`,
  `cache_provider_family`, `cache_requested`, `cache_skip_reason`,
  `context_window`, and `context_window_source` onto every ledger row, the
  `get_last_usage()` record, and the unified `model_routed` trace event.
  `cache_status` is one of `hit|partial|creation|miss|unsupported|unreported`;
  `unreported` means the provider reported no cache field and is deliberately
  NOT counted as a hit or a miss. When a provider reports cached tokens but
  no cost, the fallback estimate prices the cached portion at
  `prompt_cache.DEFAULT_CACHE_READ_DISCOUNT` and the row's `cost_source`
  becomes `...+cache_discount` so the estimate is never mistaken for an
  invoice. New `runtime/model_router.py::get_cache_summary()` and the config
  keys `prompt_cache` (default True), `prompt_cache_min_prefix_tokens`,
  `prompt_cache_breakpoint`, `context_window_probe` (all additive and
  task-config driven).
  (2) NEW `runtime/model_capabilities.py` resolves a model's context window
  and caches it per `(api_base, model)`; the ladder is injected probe ->
  litellm model info -> local table -> `FALLBACK_CONTEXT_WINDOW` (8192). An
  unknown window is NEVER zero, and the resolved `source` is always reported.
  No network call is made by this module: a live probe must be supplied by the
  caller. No `INTERFACES.md` signature changed.
  (3) `harness/prompts.py`: `STEP_SYSTEM_TEMPLATE` is reordered static-first
  (the rendered text is unchanged, only its order), with the per-turn half
  starting at the new `STEP_CACHE_BREAKPOINT` constant. New
  `step_per_step_data`, `split_step_system`, and `render_step_messages`
  (returns `[system(frozen), system(per-turn), user]`). `render_step_system`
  and `render_first_user` keep their exact signatures and content contract.
  `harness.core.run_step` was NOT switched to `render_step_messages`: see the
  disclosed blocker in `runtime/AGENTS.md`.
  (4) `harness/retrieval.py::retrieve_context` gained the additive keyword
  `cache: bool = True` and keeps its historical FOUR-KEY return value
  exactly. The cache is keyed by request and re-validated against the current
  content digest of every file the cached result cited, so a hit can never
  serve a ranking derived from edited bytes; the receipt (`hit|miss|refreshed|
  disabled`) rides the `retrieval_context` trace payload and the new
  `harness.retrieval.context_cache_stats()` / `clear_context_cache()`.
  `load_code_graph(repo)` without an `index_root` now uses the code graph's
  own persistent per-repository root instead of a throwaway temp directory,
  so `load_or_build`'s per-file content-digest check actually reuses the
  tree-sitter parse.
  (5) NEW `execution/warm_sandbox.py`: `WarmTaskSandbox` runs one long-lived
  container per `(repository, task)`. `SandboxIdentity` is the reuse key and
  is enforced — a second identity while one is live raises
  `IdentityMismatch` instead of sharing. `purpose="verification"` is REFUSED
  (`WarmSandboxError`): the final verification boundary keeps a fresh
  container per command through `execute_sandboxed`, which is what mints
  verified success. `hostile=True` (or `reuse=False`) routes every command
  through `execute_sandboxed`, so per-command isolation stays available
  without changing call sites. Warm container names keep the
  `hexec-e<env>-p<pid>-` prefix (plus a `w` marker), so the existing orphan
  reaper and residue assertions apply unchanged. Re-exported from
  `execution/__init__.py`.
  (6) CLI: `cli/interactive.py` gained `trace_cache_summary(trace_file)` and
  `session_cache_total(log_root)`, and `/cost` renders one cache line for the
  run and one for the session. `cli/main.py::_result_json` gained a
  `prompt_cache` block. A run whose provider reported no cache usage renders
  `no cache data`, never a 0% hit rate.
- 2026-09-26 (Ceiling Terminal 05 — context compiler, symbols, LSP, memory
  capture): **the compiled context bundle now reaches every model request on
  the kernel path; the canonical tool catalog gains four read-only symbol
  tools and one permission-gated memory tool; `harness.lsp` gains one
  convenience method; `mcp_server.record_decision` gains additive optional
  parameters.** (1) NEW `harness/knowledge.py::KnowledgeContext` is the
  run-scoped binding that owns exactly one `ContextCompiler`, one
  `LspManager`, and one `DecisionStore` per run. `compile()` is idempotent
  and returns a receipt carrying `sources`, `tokens`, `chars`, and
  `compaction_metadata`; `context_block()` returns the bounded block that
  the daily strategy injects. `harness/agent_kernel/strategy.py` gained
  `knowledge()`, `_compile_knowledge()`, `_knowledge_event()`,
  `_close_knowledge()`, and `_observe_lsp_after_mutation()` as ADDITIVE
  methods: `_metadata_context` now appends the compiled block, and because
  the strategy seeds its `[system, user]` frame once per run, that block is
  a prefix of EVERY later model request. New trace events: `knowledge_bound`,
  `context` (sources + token cost + reversible-compaction metadata),
  `lsp_diagnostics`, `lsp_diagnostics_observed`, `lsp_observation_failed`,
  `knowledge_close`, `memory_recorded`, `memory_record_refused`,
  `memory_record_deduplicated`, `knowledge_unavailable`. No existing event
  was removed or renamed.
  (2) `harness.tools._TYPED_TOOL_SPECS` gained `read_symbol`,
  `find_definition`, `find_references`, and `blast_radius` (all
  `read_only`) and `memory_record` (`memory`, `requires_approval=True`).
  The kernel derives its tools from this catalog, so all five are
  automatically available to the model and to `catalog_parity_report`. The
  store's per-task dedupe does not span sessions, so repository- and
  category-scoped dedupe lives in `KnowledgeContext.record_memory`; a
  duplicate returns the FIRST record's id instead of writing a second row.
  (3) `harness.lsp.LspManager` gained `sync_document(path, text=None,
  version=None) -> bool` (didOpen on first call, didChange afterwards) and
  `__all__` is unchanged. (4) `harness/retrieval.py` gained
  `find_definitions`, `find_references`, `read_symbol_records`,
  `read_symbol_by_search`, `blast_radius_for`, and `retrieval_ablation`.
  Every one reports `available: False` / `resolution: "name_based"` when the
  index cannot be read, so a cold index is never reported as "the symbol does
  not exist". (5) `mcp_server.record_decision` gained additive optional
  `session_id`, `model`, and `dedupe=True` parameters and records the capture
  `timestamp` in the provenance block; the tool COUNT is unchanged at five,
  because `tests/test_mcp_server.py` and `tests/test_memory_mcp_release.py`
  pin the exact five-tool surface. (6) New config keys, all with defaults in
  `harness/config.py`: `knowledge_enabled` (True; the single OFF arm),
  `knowledge_block_max_chars` (24000), `knowledge_tool_max_chars` (6000),
  `knowledge_diagnostics_max_chars` (2000), `context_token_budget` (12000),
  `context_chars_per_token` (4), `context_cache_entries` (32),
  `context_recent_turns` (6), `context_map_symbols` (25),
  `context_dependency_limit` (20), `decision_memory_enabled` (True),
  `lsp_enabled` (False), `lsp_timeout_s` (5.0), and
  `memory_record_enabled` (True).
- 2026-09-26 (Ceiling Terminal 04 — native tool protocol and one canonical
  catalog): **Boundary 2 gains an additive tool-schema parameter and a
  normalized native return; the kernel tool catalog is deleted in favour of
  the production one.** (1) `runtime.model_router.call_model` gains
  keyword-only `tools` and `tool_choice`. When `tools` is supplied the
  provider-neutral schemas are forwarded to litellm verbatim, and a provider
  that answers with native tool calls returns
  `{"content", "text", "tool_calls", "finish_reason"}` instead of a bare
  string; **a response with no native call still returns the historical
  string**, so every existing Boundary-2 consumer is unchanged. Each attempt
  record gains additive `stop_reason` and `tool_calls` keys, and an empty
  completion now records the provider's `finish_reason` before raising.
  (2) `harness.model_client.ModelClient.call` gains a `tools` keyword
  forwarded only when supplied (signature-adaptive, so a pre-tool-protocol
  boundary keeps working); `call_structured` now returns the response
  untouched instead of re-wrapping it. (3)
  `harness.agent_kernel.builtin_tool_specs()` is derived from
  `harness.tools.typed_tool_specs()`; the hand-written kernel tool list is
  gone. New public helpers: `harness.tools.canonical_tool_names`,
  `canonical_tool_aliases`, `catalog_fingerprint`, `catalog_parity_report`,
  and `harness.agent_kernel.tools.catalog_parity` /
  `catalog_identity` / `catalog_specs`. `ToolRegistry.schemas()` returns the
  canonical schemas filtered to the registry, and `restrict()` is alias-aware.
  The canonical catalog gained `verify` and `cancel`, `test` accepts both
  `command` and `test_command`, and the input-request tool's canonical name
  is now `ask` with `question` as its alias. (4) `ToolRegistry` gained
  digest-bound mutations: `read` appends a `[neo-file-digest]` trailer, a
  mismatched `expected_revision`/`expected_revisions` is refused with the
  stable `stale_read` slug, a missing digest is bound from the session's own
  earlier observation (never a dispatch-time hash), an ambiguous `edit` is
  refused as `ambiguous_match` and a missing one as `no_match`, repeated
  identical calls are refused as `loop_detected` after
  `max_repeat_tool_calls` (default 2, read-only exempt), and
  `failure_report()` tracks protocol failures separately from task failures.
  `ToolResult` gained additive `error_kind` and `digest`; `ModelResponse`
  gained `tool_protocol`, `stop_reason`, `protocol_errors`, and
  `duplicate_events`. New config keys: `max_repeat_tool_calls`,
  `loop_guard_read_only`, `require_edit_digest` (all read from `Task.config`
  / kernel config, defaults preserve current behavior). (5) Cross-module
  integration: `runtime/roles.py` resolves catalog aliases so a role may name
  a tool the way a model does, and
  `tests/test_workspace_security.py` asserts the `question` alias resolves to
  the canonical `ask` spec. No `Task`, `TaskResult`, `ExecutionResult`,
  `VerificationResult`, or `RunResult` field changed; the verifier still mints
  verified success and `completed_unverified` is unchanged.
- 2026-09-25 (Terminal 13 security and trust ceiling): **the security helpers
  become production call sites; four observable contract changes, all
  additive.** (1) `harness/trace.py` no longer forks redaction — the
  authoritative `trace.jsonl` now emits `shared.security.REDACTED_SECRET`
  (`[REDACTED_SECRET]`) instead of its private `[REDACTED]`, and
  `harness.trace.redact_secrets` IS `shared.security.redact_secrets`;
  `TraceLogger` gains `write_receipt()`/`read_receipt()` writing a separate
  `logs/{task_id}/receipt.json`. (2) `harness.webfetch` is deny-by-default:
  `FetchResult` gains `egress_reason` and `untrusted` (defaults, so existing
  4-field construction still works) and two new status slugs
  `egress_denied` / `untrusted_blocked`; `fetch_webpage`/`fetch_and_render`
  gain keyword-only `allowed_hosts` / `egress_policy` / `untrusted_mode`.
  The default allowlist is `pypi.org`, `files.pythonhosted.org`,
  `docs.python.org`, `example.com`; `NEO_EGRESS_ALLOWED_HOSTS` overrides it.
  (3) `runtime.approval` requests carry additive `effect` and `effect_digest`
  keys, `request_approval` gains keyword-only `command`, and a new
  `ApprovalStale` exception (plus `recheck_effect`) is raised instead of
  returning `"approve"` when the stored effect changed — callers that catch
  only `ApprovalRejected`/`ApprovalTimeout` must add it. (4)
  `extensions.PluginManifest` now raises `PluginManifestError` when a
  manifest `description` or `metadata` string is quarantined by the
  untrusted-content policy; previously any string was accepted.
  New shared modules: `shared/approval.py`, `shared/egress.py`,
  `shared/supply_chain.py`, `shared/security_advisories.py`. Extended:
  `shared/security.py` (untrusted-source boundary, memory write gate, run
  receipts), `shared/threat_model.py` (egress, approval-integrity,
  provenance, sandbox-escape threats + 4 new invariants).
  `shared.security.detect_prompt_injection` gained a negation scope so honest
  guardrail prose is not flagged; structural and identity rules are never
  negated, and the drive-letter traversal rule no longer matches the `s:/`
  tail of `https://`. `execution.sandbox` gained
  `assert_sandbox_argv_isolated` (pre-spawn isolation gate),
  `image_digest`/`pin_image`/`prepull_base_image`,
  `declared_egress_allowlist`, and `prune_sandbox_artifacts`.
  `mcp_server` guards every returned payload and gates `record_decision`.
  `harness/skills.py` reviews every skill body and reports quarantines.
  No `Task`, `TaskResult`, `ExecutionResult`, `VerificationResult`, or
  `RunResult` field changed; the verifier still mints verified success.
- 2026-09-25 (Terminal 09 packaging/Windows uninstall closure): **the existing
  `neo uninstall [--yes|--dry-run]` command keeps its CLI signature and exit
  codes, but Windows plain-pip self-uninstall is now asynchronous and
  fail-closed.** When a metadata-owned `neo.exe`/`harness.exe` launcher is an
  active ancestor, the command starts a fixed-argv detached helper with an
  interpreter outside the active venv. The helper opens and validates the exact
  launcher process object before the command returns, waits for it to exit, then
  runs `<venv-python> -I -m pip uninstall -y <neo-agent-cli|neo-agent-cli>` and
  writes an atomic private receipt. Scheduling failure performs no config/PATH
  cleanup. The 0.2.1 artifact also adds the existing `agent_sdk` package to the
  wheel/sdist; clean installed-wheel SDK query/replay and real Windows
  launcher-uninstall regressions are release gates. No task/runtime/execution/
  memory boundary signature changed.
- 2026-09-25 (Terminal 07 TUI/REPL/headless parity): **CLI-internal shared
  command/event contract; no harness, runtime, execution, memory, MCP, journal,
  session, or exit-code boundary changed.** `cli.commands` now defines the nine
  normalized surface states, shared command context, and version-1
  command-started/state-changed/command-finished outcome events. REPL, TUI, and
  `neo run --json` resolve the same `CommandSpec`; their result records carry
  equivalent status, state, verifier facts, recovery, and exit meaning. Journal
  resume receipts project as `resumed`; unverified success remains
  `completed_unverified`. TUI-only theme, hook, and builtin patches are
  mount-scoped/restored. The nine-state matrix, repeated randomized runs, full
  TUI suite, and attached-PTY campaign are recorded in
  `logs/terminal-ux/terminal-07.json`.
- 2026-09-25 (resume identity closure): **additive checkpoint identity fields and
  fail-closed resume validation.** Runtime `checkpoint.json` now records a
  versioned canonical repository path, SHA-256 request identity, repository
  revision identity, and scheduler resume namespace; the worker rejects
  legacy or mismatched checkpoints and emits a trace event. The shared
  `Checkpoint` contract adds the same repository/request/revision/namespace
  fields, and strict daily, verified-fix, and legacy-agent strategies validate
  them before consuming a checkpoint. The explicit `continue` compatibility
  request is the only request alias; arbitrary request changes are rejected.
  Focused runtime, tracing, scheduler, kernel, and SDK verification is recorded
  in the runtime handoff.

- 2026-09-25 (final integration hardening): **approval request/decision
  identity is now explicit and fail-closed; all existing positional callers
  remain valid.** `runtime.approval.request_approval` adds optional
  `repo_path=""`; persisted requests include a SHA-256 fingerprint over task,
  canonical repository, issue, and exact diff plus a random `request_id`, and
  decisions must carry both matching fields. Mismatched/legacy requests rotate
  and stale decisions are ignored. `shared.traceview` and `neo status` now
  accept canonical Boundary-0 `event`/`payload` rows and nested strict terminal
  results while preserving legacy `kind`/`data` readers. Strict model gateways
  read module-level router usage and expose both `cost` and `cost_usd`.
  Packaging aligns installers/docs to Python 3.10-3.12 and ships help entry
  points for `harness`, `cli`, `runtime`, `execution`, and `evals`. Focused
  verification: 60 passed, installer suite 19 passed, scoped Ruff/format clean;
  fresh Python 3.10 wheel/sdist installs and smoke checks passed. Resume/
  checkpoint identity binding remains an explicit unfinished blocker.

- 2026-09-25 (Terminal 05 TUI/REPL product shell): **CLI-internal additive
  projection and product surfaces; no cross-module signature or schema change.**
  `cli.commands` now registers six product modes and metadata-only command
  policies; explicit mode runs use the existing Boundary-0 `AgentKernel` and
  `RunSpec` while compatibility dispatch remains intact. `cli.runview` and
  `cli.tracelog` read both legacy and normalized journal rows. `/mode`,
  `/files`, `/checkpoints`, `/diagnostics`, `/redo`, `/export`, and `/share`
  are shared REPL/TUI surfaces; exports are privacy-filtered and approvals
  preserve once/session-path/session-command scopes. The authoritative
  `trace.jsonl` remains the source of run state; captured backend text is
  diagnostic-only in the TUI. Required suite: 256 passed, 0 failed; Docker and
  live-provider lanes were not run. Handoff:
  `logs/architecture-round/terminal-05.json`.

- 2026-09-25 (Terminal 04 durable sessions/checkpoints): **additive CLI/memory
  persistence contracts; no Boundary-4 six-key change and no sixth MCP tool.**
  `cli.session` now owns versioned atomic snapshots with an append-only
  conversation event journal, explicit corruption/recovery receipts, workspace
  identity, run-linked turns, fork/resume/export/import, and explicit
  provenance-gated decision promotion.   `memory.checkpoints` exposes named
  shadow-Git/worktree checkpoints, review/diff, and conflict-safe file or
  file-plus-conversation restore. Restore validates snapshot hashes and
  containment, honors selected-file scope, and quarantines storage symlinks;
  fresh session recovery quarantines both snapshot and event journals.
  `memory.mcp_client.McpClient` owns one typed stdio lifecycle with bounded
  health and cancels timed-out synchronous workers; `mcp_server.server_health()`
  is a local non-tool health projection. Existing `list_mcp_tools` and
  `call_mcp_tool` shapes remain compatible. Cross-owner integration requests
  for the kernel mutation barrier and CLI connector health wrapper are recorded
  in `logs/architecture-round/terminal-04.json`.

- 2026-09-25 (Terminal 1 architecture kernel): **new Boundary 0 defines
  `shared/agent_contracts.py` as the canonical versioned agent contract.**
  `SessionState`, `RunSpec`, `ToolCall`, `PermissionDecision`, `RunEvent`,
  `Checkpoint`, `RunResult`, and `CompletionStatus` all serialize with
  `schema_version=1`; the seven required completion values are exact and
  unknown versions/statuses fail closed. `AgentKernel` now owns the run
  lifecycle and one contiguous `trace.jsonl`; `replay_run(path)` is a pure
  deterministic projection with sequence/identity/version validation.
  Registered strategies are `daily`, `verified_fix`, `planning`, `question`,
  `research`, plus the private-loop compatibility adapter `legacy_agent`.
  `harness.core.run_task` and `harness.agent_loop.run_agent` remain the public
  compatibility facades and preserve `TaskResult`, `AgentResult`, and the
  stable six-key `state.json` prefix. No CLI-private import exists under
  `harness/agent_kernel`.   Verification: required 3-file suite 111 passed; real-Docker
  `tests/test_e2e_run_task.py` 28 passed; `tests/test_modes.py` 99 passed;
  `tests/test_scheduler_integration.py` 38 passed. Remaining cross-owner
  requests are recorded in `logs/architecture-round/terminal-01.json`.

- 2026-09-25 (Terminal 11 daily-driver integration): **additive MCP,
  skill-receipt, session-context, and readiness contracts.** Flat MCP model
  calls retain their outer `server`/`name`/`args` payload through
  `ToolCall.from_dict` and the agent-kernel registry, while nested
  `arguments` envelopes remain distinct. Safe edit/apply-patch handlers bind
  the current workspace revision before mutation, preserving stale-edit
  protection while allowing ordinary typed calls to succeed. The general
  agent and verified-fix path expose `skill_model_content` alongside the
  existing `skills` trace receipt, and `run_agent`/`run_agent_kernel` accept
  an optional bounded `session_context` with a `session_context` receipt.
  Daily-driver reports
  include observed 13-feature enabled/disabled evidence, explicit sampled
  manual-repair loading, Docker/live-provider lanes, and fail-closed
  readiness. Verification: focused kernel/eval selection 97 passed;
  post-fix kernel/workspace selection 88 passed; the current affected
  harness/CLI/session suite split totals 161 passed with 1 platform skip;
  full 26-case x 2-arm daily run 52/52 probes plus Docker canary passed;
  post-fix dd_02 and dd_14 mutation cases passed all four arms; prompt host
  check 14/14 CLEAN. LSP, live-provider, and explicit manual-
  repair evidence remain honestly reported blockers rather than fabricated
  passes.

- 2026-09-24 (Terminal 4 release integration): **Boundary 5 gains
  optional repository scoping on `query_decisions(query, repo_path="")`
  and `record_decision(text, category, repo_path="")`; omitted values
  preserve the historical global behavior.** The complete five-tool
  signatures are now recorded above. `CodeGraph` defaults its persisted
  index to `HARNESS_HOME/code-graph` rather than writing `.harness` into
  the indexed repository; explicit `root=` remains supported. The prompt
  eval default is again `prompt-regression`; daily-driver and combined
  checks are explicit. Packaging declares MIT metadata, `mcp>=2,<3`, and
  the current runtime subpackages, and the new release-gate workflow
  builds/tests the wheel+sdist, installed user flow, full prompt matrix,
  and Next.js site. Version remains the unpublished 0.2.1 source
  candidate; public PyPI is still 0.2.0. Hostless verification and the
  full 14-task × 8-arm Docker prompt matrix passed; final publication
  still requires a clean reviewed tag and resolution of parallel lint debt.
- 2026-09-24 (Terminal 7 daily-driver evals): **new isolated
  20-scenario x baseline/adversarial product-readiness matrix; no harness,
  runtime, execution, memory, or CLI production-boundary signature/schema
  changes.** `evals.daily_driver` is selected explicitly with
  `python -m evals.run --suite daily-driver`; the established default
  prompt-regression suite remains unchanged. `--suite combined --check`
  runs both checks and writes a separate combined report without
  overwriting the daily report. Every daily arm runs in a subprocess with
  private HOME/config/memory/log/trace/plugin roots and an allowlisted
  environment (provider credential variables are not copied). Reports
  include fail-closed assertions/receipts, reproducers, status and
  critical-failure counters, all-sample latency percentiles, token/cost
  metrics, explicit Docker/live-provider lanes, and readiness. Real Docker
  canary passed. Latest report:
  `logs/evals/20260924-155439-9a533a46747d4bdb9d02fb00f2ed7a4d/daily_driver_report.json`
  (36/40 arms; remaining failures are typed-MCP argument loss and missing
  general-agent skill-body injection). No product code was edited by
  Terminal 7.
- 2026-09-22 (slash-surface round): **9 new built-in slash commands,
  REPL + TUI; BUILTIN_SLASH_COMMANDS grows accordingly (custom names
  can no longer shadow /init /model /login /logout /mcp /skills /cost
  /undo /clear — /model was also missing from the set).** New shared
  helpers in cli.interactive (both shells render through them):
  trace_usage_sum / session_spend_total, _render_cost/_render_skills/
  _render_mcp/mcp_server_table, _do_init (calls neoconfig writers),
  _do_logout (calls onboard.cmd_logout), _do_clear, undo_result (+
  history_matches reusing session_matches for /history). /login calls
  the onboarding wizard (REPL: cmd_login; TUI: _OnboardScreen modal);
  /mcp resolves labels via the agent loop's read-only
  _resolve_mcp_server and lists tools via memory.mcp_client
  (harness/agent_loop.py itself untouched — surface names only).
  No harness/runtime/memory contract changes; trace/state schemas
  unchanged. Tests: tests/test_cli_slash2.py (33).
- 2026-09-21 (multi-language round): **Boundary 1 now covers JS/TS —
  no signature changes, language is auto-detected.** `verify()` runs
  Jest/Vitest repos (target filter `<file> -t <name>`; baseline/
  regression/flake semantics unchanged and language-independent);
  `execute_sandboxed` builds node:22-slim images with npm deps on a
  read-only volume mount (Python image path byte-identical).
  `memory.code_graph` indexes .js/.jsx/.mjs/.cjs/.ts/.tsx via
  tree-sitter (same Graph/NodeInfo shapes — harness retrieval needs
  no changes). `Graph.canonical_defines` is the additive node-id-only
  define-edge view; historical `Graph.defines` qualified aliases remain
  readable for compatibility. Tests: tests/test_verify_js.py + test_multilang_graph.py.
  SCOPE FLAG: project-spec.md "explicitly out of scope" still says
  multi-language support beyond Python — that line is stale as of this
  round and needs an amendment; not silently rewritten here.
- 2026-09-21 (agent demo-parity round): **agent resume replays history
  (not a restart); _resume_task returns its result; run_agent gains an
  additive resume_history kwarg.** `harness.agent_loop.run_agent(...,
  resume_history=None)` injects prior-session context as a steering
  user message (at=agent-resume-history) and emits the same steering
  trace kind the plan-guidance path uses; new
  `load_resume_history(task_id, log_root)` rebuilds the preamble from
  the prior trace (request + files + recent exchanges, never raises).
  `cli.interactive._resume_task` returns the run's result dict (fix
  path: _execute_task's; agent path: same task id, pristine/orig kept,
  prior trace + conversation turns replayed) and both REPL /resume
  branches fold it via `_fold_resumed` into `last` + the conversation
  transcript. `_run_one_agent` gains the additive `resume_history`
  kwarg (legacy fakes without it fall back without replay). TUI:
  /diff recomputes the live agent diff when last[] is empty (REPL
  parity), _start_resume records the /resume user turn, and the
  resume worker folds the returned dict via _note_result. New
  `demo/agent_demo.py` (offline scripted 7-step demo). No fix-mode
  contract changes (core/editor/tools/verify/sandbox untouched).
- 2026-09-21 (first-run onboarding round): **no model set + `neo` ->
  inline wizard (once), saves, never asks again. CLI-internal only,
  no Boundary signature changes (fix-mode run_task/verify/sandbox/
  state.json untouched; the harness never sees the wizard).**
  New `cli/onboard.py`: PRESETS (Official OpenAI/Anthropic/Gemini via
  litellm names + Router OpenRouter/TokenRouter/Ollama/Custom with
  free-text base_url + model name — never a hardcoded-only provider
  list), detection over the effective resolution (flags > env >
  local > project > global > legacy; local loopback endpoints need
  no key), `run_repl_wizard` (pick -> editable base_url -> model
  with suggestions + free text -> masked api_key -> ONE tiny live
  litellm call; fail = error + retry, bad creds never saved), save
  discipline (api_key+base_url ALWAYS global; model global unless
  --tier project; official picks clear stale router bases),
  `neo login [--tier]` / `neo logout` (strip key, keep model/base),
  `/model` display (+ source tier) in REPL and TUI, flag-command
  gate (missing creds -> honest stderr + exit 4, never prompts;
  --json emits a parseable error doc; fake/mock/scripted models
  exempt). `cli/tui.py`: `_OnboardScreen` modal (same stepped flow;
  test call off the UI thread; Esc skips) auto-pushed once at
  session start via an explicit `onboard_prompt` flag from run_tui
  (a mount-time isatty probe fires under Pilot — textual swaps
  sys.stdout — so direct NeoApp(...) construction stays modal-free).
  `cli/neoconfig.py`: `set_tier_key` refuses project-tier api_key
  (would be committed), settings writes chmod 600 best-effort
  POSIX. `cli/interactive.py`: REPL session-start hook (once;
  pipe-safe) + `/model`. `cli/main.py`: `login`/`logout`
  subcommands; `fix` + `run-benchmark` gates. Skips: `/skip`,
  NEO_NO_ONBOARD=1, non-TTY, --json. Tests:
  tests/test_cli_onboard.py (detection, save tiers, masking,
  wizard incl. wrong-key-saves-nothing + retry-fix, gates incl.
  --json doc, login/logout, TUI modal incl. full Pilot save flow);
  test_cli_errors crash pin + test_cli_adversarial injection pin
  updated for the exit-4 contract (creds supplied / 4 allowed —
  intents unchanged). Known environmental note: Docker daemon down
  machine-wide at round time — Docker-backed e2e (offline fix,
  JsonMode, scripted interactive) fail at baseline verify before
  any touched code runs (trace-proven); re-run when back.
- 2026-09-21 (agent trust round): **the agent loop is daily-driver safe:
  TUI approval modal, agent plan preview, undo hardening, MCP/plugin/fetch
  tools. No Boundary signature changes (fix-mode run_task/verify/sandbox/
  state.json untouched; `neo fix` path byte-identical); additive
  harness-internal + CLI-internal surface only.** `harness/agent_loop.py`:
  new tools `fetch` (JSON + FETCH line; GET-only SSRF-guarded webfetch,
  `agent_fetch_enabled`/`agent_max_fetches`, budget exhaustion nudge —
  BASH with a FETCH-shaped command is redirected, never shelled),
  `mcp`/`mcp_call` (installed plugin servers + config
  `agent_mcp_servers`; failures degrade to TOOL ERROR kinds, never a
  traceback), plugin verbs via one `route_tool` (builtin|mcp|plugin|
  unknown; read-only verb shapes auto-run even in require mode,
  non-conforming shapes rejected, unknown tools honest errors);
  `parse_tool_call` accepts plugin first-tokens via optional
  `known_verbs` (default strict — the `{"tool":"nuke"}` pin holds);
  `render_agent_plan` (heuristic steps+files, no verifier fabrications;
  approved plans inject as `plan_guidance` steering, never a contract);
  BASH runs LIVE (local stub, never Docker; respects injected fakes;
  deny-guard + cwd tracking kept; Ctrl+C stops the call, not the
  session); `undo_edits` gains per-file `targets=` + appends an `undo`
  trace event (pristine/ never touched; resume keeps pristine/orig so
  undo stays coherent). New config keys (all additive):
  agent_fetch_enabled, agent_max_fetches, agent_live_bash,
  agent_mcp_servers. `cli/interactive.py`: `_run_one_agent` gains
  `approve_fn_override` + `plan_guidance`; `/plan <text>` classifies
  (agent-shaped -> lightweight preview with approve/edit-steer/cancel,
  else the fix-loop preview); `/diff undo <file|all>` restores exactly
  that file; REPL approver unchanged (per-call) + NEO_NOTIFY bell.
  `cli/tui.py`: `_agent_approve_fn` (same _ConfirmScreen pattern:
  diff+command body, y=once / a=always-latched / n=safe-default,
  NEO_NOTIFY bell); `_agent_worker` passes it + pending guidance
  (signature-inspected so legacy test fakes keep working); `/plan`
  agent preview with approve/edit modals; `/diff undo <file|all>`.
  `cli/tracelog.py`: FETCH/MCP feed labels (additive; fix loop never
  emits them). Tests: test_agent_loop 56 (plan/fetch/MCP/plugin/undo/
  live-bash/REPL-preview), test_cli_tui 47 (+4 approval-latch unit);
  live drive (require approve+deny+undo+diff, plan->edit->DONE, real
  `python -m mcp_server` query_decisions through the loop); evals
  --check OK; ruff clean on new/edited regions (interactive/tui held
  at pre-existing baselines).
- 2026-09-21 (first-run .neo/ scaffold round): **first `neo` in a repo
  auto-scaffolds the project layout; `neo config init-project` is the
  explicit form of the same scaffold. CLI-internal only, no Boundary
  signature changes.** `cli/neoconfig.py`: new `find_git_root`
  (nearest `.git` ancestor; the outside-a-repo gate), new
  `ensure_project_layout` (creates settings.toml + key-less
  settings.local.toml + `commands/fix.md` + `skills/code-review/
  SKILL.md` examples — only missing pieces, never overwrites; example
  dirs are created only when the dir itself is new, so a deleted
  example stays deleted), `ensure_project` keeps its
  `(created, path)` signature and now scaffolds the full layout, new
  `maybe_scaffold_repo` (git-root detection with $NEO_PROJECT_DIR
  override; best-effort, never raises — the session entry points call
  it after `ensure_first_run`: REPL in cli/interactive.py, TUI in
  cli/tui.py, both with a one-line `repo setup:` notice only when
  something was created). Broken-TOML/unreadable/wrong-type warnings
  are now once-per-process per file (`_warn_once` — `config list`
  re-reads the chain per key and used to spam one warning per key).
  `neo config init-project` (cli/main.py) reports the extra scaffolded
  files. Installers verified package-only (no config writes; banners
  already read "the AI coding agent for your terminal"; python>=3.10
  hard-fail, git/Docker warn-only on the PyPI path, idempotent PATH,
  post-install `neo --version` + `neo update --check`). Tests: 10 new
  `TestProjectScaffold` in tests/test_cli_config.py (52/52 file
  green), incl. a real-`git status --untracked-files=all` pin
  (settings.toml visible, settings.local.toml ignored). Session-entry
  drivers in tests/test_cli_neo3.py + tests/test_modes.py now
  `monkeypatch.chdir(tmp_path)` so they can never scaffold the real
  tree. Known edge: walk-up repo detection treats a home-dir dotfiles
  git repo as a repo (consistent with the existing project-tier
  walk-up); $NEO_PROJECT_DIR overrides detection.
- 2026-09-21 (general-agent round): **interactive `neo` is now a general
  coding agent (ONE live-repo loop); `neo fix` UNCHANGED. No Boundary
  signature changes (fix-mode run_task/verify/sandbox/state.json all
  byte-identical); new harness-internal + CLI-internal surface only.**
  New module `harness/agent_loop.py`: `classify_agent_input` (question |
  agent_task | chit_chat — deterministic rules + ONE cheap model call
  for the gray zone, same tier discipline as harness.intent),
  `run_agent` (generic READ/GLOB/GREP/BASH/EDIT/WRITE/MEMORY/VERIFY/
  DONE tool loop on the LIVE repo via the existing
  execute_sandboxed/BashSession + editor + decision-memory surfaces;
  steering journal polled every turn; NO verifier gate unless
  target_test/test_command declared; pristine/ snapshot + orig/ per-file
  originals under logs/{task_id}/ are the diff/undo reference only),
  `parse_tool_call` (JSON or one-line form), `agent_diff`/`undo_edits`.
  New config keys (all additive): agent_max_turns (25),
  agent_approval ("auto"; "require" gates BASH/EDIT/WRITE behind
  approve_fn), agent_context_files/lines, agent_max_read_chars,
  agent_intent_enabled. Session dispatch (cli/interactive.py +
  cli/tui.py) uses the agent dispatcher: questions -> the unchanged
  read-only _run_one_question; everything else work-shaped ->
  _run_one_agent (live repo, diff + /diff undo, steering registration,
  session index). Legacy `_run_one_fix/_run_one_build/_run_one_research`
  + `neo fix` + harness.router/intent stay for the benchmark path
  (untouched); /plan + /review-with-template intentionally stay on the
  fix loop (preview machinery lives in _execute_task). FeedBuilder
  renders the new tool_call verbs + edit_applied/approval events
  (additive). Tests: tests/test_agent_loop.py (32); session-wiring
  suites updated to the 3-way contract (test_modes TestSessionWiring,
  test_cli_neo3 bug-sentence, test_cli_tui dual-fake + agent dispatch +
  direct fix-worker preview tests, test_cli_plugins generic-custom-
  command). Known limits: TUI agent_approval=require has no modal yet
  (tools refused honestly); research-shaped input is answered via the
  repo-grounded question path (no web FETCH there — use the legacy
  research entry programmatically).
- 2026-09-21 (agent-session round): **persistent conversation sessions;
  CLI-internal only, no Boundary changes.** New module `cli/session.py`
  (one state file per conversation under `<log_root>/_conversations/`:
  transcript turns + input history + compacted summary; `@path`
  expansion; `compact_session` reusing the existing
  `TraceLogger.find_events` recall primitive; memory-first
  `session_memory_brief`/`ingest_session_facts`; best-effort
  `copy_text_to_clipboard`). `cli/commands.py::BUILTIN_SLASH_COMMANDS`
  gains `/plan`, `/compact`, `/copy-diff`, `/copy`, `/trace`, `/feed`,
  `/steer` (new builtins a custom command can no longer shadow);
  `/review` deliberately stays OUT so project/plugin `review.md`
  templates keep working (bare `/review` renders diff + rationale,
  `/review <args>` with a template runs it). `record_session`
  (cli/interactive.py) now also ingests facts into memory. New
  interactive commands in both REPL and TUI: `/plan`, `/review`,
  `/compact`, `/copy-diff`, `/resume` with no id (most recent
  resumable). NOTE (parallel session, same tree): the agent-loop
  migration (`harness.agent_loop`, `_run_one_agent`) is in flight;
  its stale `test_slash_custom_command_without_arguments` pin
  (monkeypatches `_run_one_fix`, code now calls `_run_one_agent`)
  fails independent of this round.
- 2026-09-21 (packaging/installer round — v0.2.0): **PyPI-first
  installs; one behavior change in cli/selfupdate.install_source().**
  `install_source()` now returns the PyPI spec `neo-agent-cli` by
  default; a git URL only when `NEO_INSTALL_SOURCE` is set or
  `NEO_INSTALL_REPO`/`NEO_INSTALL_REF` is explicitly set (previously
  always `git+https://github.com/<repo>@<ref>`). `neo update` for
  pip/venv methods therefore runs `pip install --upgrade neo-agent-cli`
  instead of the git URL; pipx (`pipx upgrade neo-agent-cli`) and the
  source-checkout refusal are unchanged. `latest_available_version()`
  now checks PyPI JSON first, GitHub tags as fallback (order swapped).
  The three installers (install.sh/.ps1/.cmd) default to the same PyPI
  spec with the same env-var git fallback; banners read "the AI coding
  agent for your terminal"; Docker is warn-only, git required only for
  git-URL sources; post-install runs `neo update --check`
  (best-effort). pyproject 0.2.0 gains `pygments>=2.13` and
  `tomli>=2.0; python_version<'3.11'` deps + `[tool.setuptools.
  package-data] cli = ["fixtures/**/*"]` (smoke fixture now ships in
  the wheel). No Boundary signatures changed; `neo --version` still
  pinned to pyproject by tests/test_cli_release.py.
- 2026-09-14 (Terminal 1+4 joint, mid-task steering round): **new
  mid-task steering surface; NO Boundary changes (purely additive
  harness-internal + CLI-internal surface).** New module
  `harness/steering.py` — the transport is the append-only journal
  `logs/{task_id}/steering.jsonl` ({"op": "inject"|"consume", ...}, one
  JSON line each; the loop's SteeringBuffer RE-SCANS the journal on
  every poll so ANY process — REPL reader thread, TUI, a second
  terminal, a test driver, an MCP client — can inject by appending).
  Loop consume points (harness-internal, in core.py): turn boundaries
  (guide → USER STEERING message in the live session; strong intents
  yield the step), step boundaries (abort → clean resumable stop;
  replan → shared re-plan path keeping work/), the final gate, and a
  pre-mint re-check — pending steering at ANY success point blocks
  success minting (verifier-gated completion never shortcut; the fix
  is re-verified after incorporation). Steering consumes as
  NON-transition state-machine events (`record_event`) — replan rides
  existing editing→repairing→planning edges; abort lands failed. The
  journal replays on construction: an unconsumed inject stays pending
  across a crash/resume (the resume contract is unaffected). New
  additive config keys: `steering_enabled` (True; False = the OFF arm,
  buffer never constructed), `max_pending_steering` (16; over-cap
  injects refused honestly), `steering_max_chars` (4000). New
  trace kinds (additive, same {ts,kind,data} schema): `steering`,
  `steering_abort`, `steering_replan`, `steering_step_yield`,
  `plan_replaced` (a steering re-plan's replacement). CLI surface
  (Terminal 4, cli/interactive.py + cli/tui.py): plain text while a
  run is live steers it (REPL via the `_ReplReader` stdin-owner
  thread + `_LIVE_RUN` registration around every `_execute_task`/
  `_run_one_build` call; TUI via in-flight plain text + `/steer`);
  `steer_live_run(line, task_id, log_root, source, say)` is the
  public inject helper (intent parsed at inject time:
  guide/replan/abort; conversational input never injected). Tests:
  tests/test_steering.py 41/41 (incl. Docker-gated e2e through the
  real loop), tests/test_cli_tui.py 33/33.
- 2026-09-14 (Terminal 1, proactive codebase health scan round): **new
  read-only scan mode + Task-C handoff; NO Boundary changes.** New
  module `harness/scan_mode.py` with the public entry
  `run_scan(repo_path, config=None, log_root=None, task_id=None,
  remote=None, focus=None, max_findings=None) -> dict` (status
  "success"|"error", scan_id, ranked findings with `index` stamped,
  shown/suppressed, notes, counts, report/scan/trace paths — read-only:
  no shell/sandbox/model; findings from the code graph + stdlib AST +
  dependency manifests, deterministic ranking, no invention).
  Consumers of existing Boundaries: coverage detection consumes
  memory.code_graph via harness.deps (load_or_build with the index
  root OUTSIDE the scanned repo); finding resolution consumes
  memory.paths.safe_task_dir for scan-id validation. New additive
  config keys: `scan_max_findings` (8), `scan_remote_deps` (False),
  `scan_pypi_timeout_s` (10), `scan_smells_per_kind` (3),
  `scan_func_gap_max` (3). New artifacts (harness-owned, inside the
  logs root): `logs/scan-<id>/scan.json` (ALL ranked findings; the
  report shows the top slice), `report.md`, `trace.jsonl` (events:
  task_start(mode=scan), scan_detector×3, scan_summary, task_end).
  New CLI surface (additive): `neo scan --repo <path> [--focus
  coverage|smells|dependencies] [--remote] [--max-findings N] [--json]
  [--fix N]` and `neo fix --finding <scan_id>#<n>` (also `neo scan
  --fix N`) — resolves a finding from scan.json and dispatches through
  the EXISTING verifier-gated entries (plain fix loop with a
  not-yet-existing target test / build mode for version-floor
  dependency bumps). T3 (runtime): no scheduler changes — a scan runs
  in-process, no workers; T4 (memory): scan.json/trace.jsonl are
  harness-internal like plan.json — no ingest changes needed. Tests:
  tests/test_scan_mode.py 50/50 (offline + mocked-PyPI remote +
  hostile-ref containment + Docker-gated e2e through the real
  run_task). Validation: full-repo run against this repo — 7 focused
  findings in 122.8s, all spot-checked genuine (the noise-budget
  iteration history: 1323 raw smell sites → capped to 3/kind).
- 2026-09-14 (Terminal 3, cross-task learning round): **new offline
  analysis job + difficulty-predictor recalibration loop; NO Boundary
  changes.** New module `runtime/analyze_history.py` (pure CONSUMER of
  the documented formats — trace.jsonl task_start/task_end/result/
  attempt_start/retrieval, `{task_id}.runtime/model_ledger.jsonl`,
  ablation summary.json; reads, never writes them) producing
  `logs/analyze-history/<ts>/report.json`: predictor-vs-outcome
  divergence (routed tasks only), retrieval-strategy-vs-repairs
  association, failure patterns, and a refit of `score_to_hint`'s
  (easy_max, hard_min) band family on a deterministic grouped per-bug
  train split with an honest held-out before/after. New CLI subcommand
  `neo analyze-history [--log-root] [--holdout-frac] [--json]
  [--apply]` (cli/main.py, additive; exit 0/2). ONE small opt-in hook
  in `runtime/difficulty.py`: `score_to_hint` now checks for
  `runtime/difficulty_calibration.json` (written ONLY by the gated
  `--apply` path when held-out accuracy actually improved); absent/
  malformed/out-of-range file = the built-in v2 bands, byte-identical
  behavior (all 8 pre-existing difficulty tests pass unmodified).
  First real run's honest verdict: recalibration MARGINAL (held-out
  0.75 → 0.75), nothing applied — details in runtime/AGENTS.md.
  Terminal 1 note: no prompt/trace-format changes are needed by this
  job, but it now READS your trace's task_start config + task_end
  status/attempts/mode fields and the attempt numbering (1-based) —
  if you ever change those shapes, this is a consumer (plus the
  dashboard, which already reads them).
- 2026-09-14 (Terminal 1, long-horizon planning for build mode): **build
  mode gains a multi-session PROJECT layer above the unchanged
  single-session build: new module `harness/build_plan.py` +
  `run_project(request_text, repo_path, config, log_root, project_id)
  -> dict` (status "success"|"checkpointed"|"already_exists"|"error"|
  forwarded sub-task failure). No existing boundary signature changed;
  `harness.build_mode.run_build` is consumed UNCHANGED as the per-sub-task
  engine (one sub-task = one run_build session on the accumulated tree).
  New artifacts (harness-owned, inside the logs root): `logs/{project_id}/
  project.json` — the PROJECT PLAN (criteria, sub_tasks, completed ids,
  current_tree, sessions, status; atomic tmp+replace writes, the
  state.json discipline) — plus pinned accumulated trees
  `logs/{project_id}.tree-s<N>/` (the verified work/ of sub-task N,
  which sub-task N+1 starts from) and per-sub-task build dirs
  `logs/{project_id}-s<N>[.base]`. New config keys (all additive):
  `build_project` (False — routes large build requests through
  harness.router to run_project instead of run_build),
  `project_max_sub_tasks` (4), `project_sub_tasks_per_session` (1 —
  the session budget on BUILDS; a sub-task that completes
  by-verification consumes none; reaching it CHECKPOINTS: status
  "checkpointed", resume with `project_resume=True` + the same
  project_id),
  `project_criteria_max` (8), `project_resume` (False). New trace
  events (logs/{project_id}/trace.jsonl): project_start,
  project_criteria_extracted/capped/parse_error/empty_reply_retry,
  project_plan_generated/capped/parse_error/empty_reply_retry,
  project_plan_saved, project_sub_task_start/end/already_passing,
  project_checkpoint, project_final_verify, project_end — safe to
  surface in dashboards. T4 (memory): project.json is harness-internal
  bookkeeping like plan.json — decisions still flow through the
  per-sub-task state.json files your ingest already reads. Tests:
  tests/test_build_plan.py (20 offline) + tests/test_build_plan_e2e.py
  (3 Docker-gated: the 2-session proof with checkpoint/resume across 3
  distinct module changes, failed-sub-task retry-from-checkpoint,
  coverage-gap abort) + the modes/e2e/evals sweeps green.
- 2026-09-14 (CLI session, live agent-trace feed round): **the full-screen
  TUI now surfaces the run LIVE — a readable one-liner-per-action feed
  (reasoning summaries, tool calls, inline diffs), built as a READ-ONLY
  VIEW over the existing trace.jsonl; no boundary signatures changed, no
  new logging path, the trace schema is untouched and the harness stays
  the single source of truth.** New module `cli/tracelog.py`:
  `FeedBuilder.consume(event) -> [FeedEntry]` (pure mapping over the
  PUBLIC trace-event kinds — same schema LiveMonitor consumes;
  `FeedEntry{summary, category, detail, detail_title, index}` carries
  the raw command+output / whole model reply as expandable detail),
  `classify_command(cmd)` (bash -> "Reading x.py"/"Running: pytest …"/
  "Editing x" texture), `summarize_reply(step, content)` (the one-line
  visible-thinking; pure-command replies produce no line), and
  `live_diff(pristine_dir, work_dir)` (inline diff from the SAME
  logs/{task_id}/pristine vs work trees the harness diffs at
  completion — same junk-skip + binary rules). The TUI's trace-tail
  thread feeds each event through the builder and renders entries the
  moment they land (verified live: <2s lag per line); edit-shaped
  entries are followed by the inline diff; `/trace` (TUI modal +
  REPL plain print) lists/expands entries and rebuilds the feed
  POST-RUN from the run's own trace.jsonl (no copy kept). The feed is
  gated on session `state["feed"]` (NOT quiet — the TUI worker sets
  quiet to silence the backend spinner); `/quiet` toggles both.
  T1 (harness): nothing needed — trace.jsonl was already the public
  observability surface; this round only READS it. Tests:
  test_cli_tracelog.py (67) + test_cli_tui.py (33, incl. 4 new) + the
  full CLI sweep (347) + a live e2e through the REAL harness loop +
  REAL Docker sandbox/verify (23/23, report at
  logs/live-feed-e2e/live_feed_report.json).
- 2026-09-14 (CLI session, full-screen TUI round): **`neo` with no
  args now launches a persistent full-screen textual App (cli/tui.py)
  — the rich-print REPL becomes the FALLBACK (no TTY / NEO_TUI=0 /
  textual absent), not the primary. No boundary signatures changed;
  the harness loop, trace schema, and approval protocol are untouched
  (the TUI renders the SAME cli.interactive backend).** New surfaces:
  (a) three embedded-UI hooks in cli.interactive — `_ON_TASK_START(task_id)`
  (fired when a task's trace dir exists; the TUI attaches its live
  run-line), `_CANCEL_RUN()` (the TUI redirects /cancel at its worker
  thread via async KeyboardInterrupt instead of SIGINT-at-main),
  `_PROMPT_BODY(prompt, body_lines)` (the backend declares the plan
  steps / diff that a blocking prompt refers to; the TUI shows them in
  the modal body). All optional; the REPL leaves them None and every
  fire is try/except'd — a broken UI hook can never take a run down.
  (b) `cli.ui.JOKES` + `ui.joke_at(i)` + `ui.THINK_FRAMES` — the
  thinking-phase treatment (distinct orbit glyph + rotating technical
  joke, Qwen-Code-style) shared by the TUI run-line and the REPL
  LiveMonitor. Dependency added: `textual>=0.40` (pyproject).
  Other terminals: nothing to ingest; `python -m cli` / `neo fix`
  flag flows are unchanged, and non-TTY no-args still prints argparse
  usage + exit 2 (CI-safe, tested).
- 2026-09-14 (Terminal 4, CLI citizenship round — release readiness):
  **EXIT-CODE CONTRACT WIDENED (Task B) + three new CLI subcommands;
  no boundary signatures changed.** The documented CLI exit codes grow
  from {0 ok, 1 task failure, 2 usage error} to
  **{0, 1, 2, 3 environment error (Docker/sandbox/deps), 4 model/
  network error, 130 interrupted}** — 0/1/2 semantics byte-identical,
  3/4 split the old catch-all 1 so CI can distinguish "fix the
  machine" from "the bug beat the agent". Source of truth:
  `cli/exit_codes.py` (EXIT_CODES table; `classify_exit_code(exc)`
  over the same layer mapping `cli.errors` documents — never raises).
  Callers that check `rc == 1` for everything keep working EXCEPT the
  env/model classes; if any script needs the old collapse, it can map
  `rc in (1,3,4)` -> failure. New subcommands (all additive, argparse):
  `neo update [--check]` (cli/selfupdate.py — install-method detection
  pipx/venv/pip/source; source checkouts get a git-pull recipe, never
  a fake upgrade), `neo completion <shell> [--install]` + the hidden
  dynamic `neo __completions` backend (cli/completion.py — candidates
  derived from the real parser, so they can't drift from --help), and
  `neo uninstall [--yes|--dry-run]` (cli/uninstall.py — enumerates
  installer-created venv/shims/PATH/config roots, confirms, removes;
  Windows user-PATH edits go through the registry API, same as
  install.ps1). `neo fix --json` / `neo status --json` (machine-
  readable stdout documents; no other output on stdout). T3/T1: no
  action needed — TaskResult, run_task, scheduler contracts untouched;
  the category split lives entirely at the CLI boundary. Tests:
  tests/test_cli_release.py (49) + updated test_cli_errors pins
  (SandboxUnavailable -> 3, unmapped -> still 1).
- 2026-09-14 (Terminal 1, Modes round — intent router & multi-mode
  capability): **NEW mode-handling contracts (Tasks A-F). All additive;
  fix-mode `run_task` is UNCHANGED — the new modes are entries AROUND
  it, never loop variants; state.json schema gains one additive key
  (`mode`, omitted for fix tasks).** The project's top-level story
  changes: a general coding agent with a rigorously benchmarked fix
  mode, not just a bug-fixing harness (see HANDOFF.md).
  - **Task A — `harness.intent`**: `classify_input(text, config,
    trace=None) -> Intent(kind: "fix"|"build"|"question"|"research"|
    "convo"|"ambiguous", reason, reply, used_model)` and
    `classify_deterministic(text) -> Intent` (offline tier). Two-tier:
    deterministic rules for clear cases (never touches the model — `hi`
    is free and never launches a task); ONE cheap model call
    (`difficulty_hint="easy"`; pin via `intent_model`) for the gray
    zone; ANY model failure/unparseable reply degrades to `ambiguous`
    (the session asks, never guesses). `intent_enabled=False` = legacy
    everything-is-fix. Config keys: `intent_enabled` (True),
    `intent_model` (None).
  - **Task B — `harness.router`**: `route(text, repo_path, config,
    log_root=None, trace=None, handlers=None) -> ModeResult` (dict:
    mode, status "success"|"failed"|"error"|"already_exists"|"reply",
    answer/result/task_id/trace_path) and `route_kind(text, config) ->
    Intent` (the single import site for the routing decision). fix →
    `core.run_task` (unchanged); question/build/research → their
    handlers; convo/ambiguous → `status="reply"` with `answer=` for the
    SESSION to print — routing never launches anything for them. The
    `handlers=` kwarg overrides dispatch (tests inject fakes).
  - **Task C — `harness.qa_mode`**: `run_question(question, repo_path,
    config, log_root, task_id) -> {answer, status, files, cost_usd,
    model_calls, task_id, trace_path}` — read-only (retrieval + decision
    memory on the ORIGINAL repo; no sandbox, no pristine/work). The
    model may emit `READ <repo-relative path>` control lines (bounded by
    `qa_max_reads`, traversal-refusing, never executed). Empty model
    reply → ONE retry with a repair nudge, then honest error. Config:
    `qa_max_files` (4), `qa_context_lines` (80), `qa_max_reads` (6),
    `qa_max_read_chars` (4000).
  - **Task D — `harness.build_mode`**: `run_build(request_text,
    repo_path, config, log_root, task_id) -> {result, status,
    acceptance_tests, already_passing, note, ...}`. Stage 1: ONE model
    call authors acceptance tests (sanitize = fix-mode agent-tests);
    written into a PRIVATE base copy at `{task_id}.base` (a SIBLING of
    the task dir — NEVER inside `logs/{task_id}/`, which `_fresh_paths`
    archives on fresh start); baseline verify must show them FAILING on
    the pristine tree (passing = `already_exists`, reported honestly).
    Stage 2: the UNCHANGED fix loop with `config["target_test"]` = the
    acceptance tests and the additive `config["mode"]="build"` →
    state.json's `mode` key (six Boundary-4 keys stay a strict prefix).
    Config: `build_tests_max` (3), `build_tests_max_chars` (16000),
    `build_tests_dir` ("tests/_build_acceptance" — reserved,
    transient).
  - **Task E — `harness.research_mode`**: `run_research(question,
    config, log_root, repo_path=None, task_id) -> {answer, fetches,
    docs, status, ...}` — read-only by construction (no shell/sandbox;
    test-pinned). FETCH/DOCS control signals bounded by
    `research_max_fetches` (4), `research_max_docs` (4),
    `research_turns` (8); every fetch audited via `web_fetch` trace
    events; degenerate FETCH lines salvaged (an attempted fetch never
    silently degrades into an ungrounded answer); empty reply → ONE
    retry, then error.
  - **Task F — the live proof**: `logs/modes-round/four_modes_session.py`
    — one session, four real inputs + "hi", real cloud model + Docker:
    **19/19 checks** (fix verifier-gated on bug02_mean; question grounded
    in numlib/mathutil.py; build's mode() green through REAL Docker
    verify against its OWN authored tests; research answering the
    num2words `to="year"` question from 4 real web fetches; "hi"
    launching nothing). Five real defects found+fixed by that session:
    build's base-copy-inside-archive-dir bug; `library`-singular
    external-marker miss; spec-language "must raise ValueError"
    misrouting build→fix; empty-reply flakes minting success in
    qa/research/build-authoring; glued-URL FETCH parse crash
    (`parse_fetch` now single-token `[^\s<]+` + URL charset).
  - New trace events: `intent` {kind, reason, tier}, `intent_model`,
    `route` {kind, reason}, `qa_read`, `research_fetch_salvage`,
    `research_empty_reply_retry`, `qa_empty_reply_retry`,
    `build_tests_*`, `build_baseline_verify`, `build_mode_summary` —
    additive, safe to surface.
  - T4 (memory): decision-memory queries on read-only modes hit the
    same `open_default_store().search(repo_path=...)` surface; nothing
    new needed. T3 (runtime): no router changes — mode handlers set
    the same Boundary-2 context (the CLI session does this per call).
- 2026-09-13 (CLI session, two-tier config + custom router round):
  **`~/.neo/config.toml` is SUPERSEDED by the two-tier settings layout —
  the legacy file still works (fallback) but is no longer the target.
  No boundary signatures changed; Task.config passthrough keys
  unchanged.** New layout: global `%APPDATA%\neo\settings.toml` (Windows)
  / `~/.config/neo/settings.toml` (POSIX; `$NEO_CONFIG` wins) + project
  `<repo>/.neo/settings.toml` (committable) + `.neo/settings.local.toml`
  (personal; auto-added to the repo's `.gitignore` by Neo itself).
  Precedence: explicit flags/session > env (`NEO_MODEL`, `NEO_PROVIDER`,
  `NEO_BASE_URL`, `NEO_API_BASE`, `NEO_API_KEY`) > project-local >
  project > global > legacy > defaults — conflict-tested at every level
  (tests/test_cli_config.py, 42). `neo config path|list|get|set|unset|
  init-project` is the management surface (`set --tier global|project|
  local`; api_key masked in output; hand-written file comments survive
  appends; broken/BOM'd TOML is warned + skipped, never crashes the CLI).
  **Custom-router support (Task B):** `base_url` is a first-class
  settings key (aliased onto runtime's existing `api_base` context key
  by `cli.neoconfig.normalize_runtime_keys`, with `provider` defaulting
  to `"openai"` — any OpenAI-compatible endpoint + any model name, not
  a fixed provider list); wiring happens in `_make_task` (flag commands)
  and `_run_one_fix` (interactive). Proven live against the real
  TokenRouter endpoint with NO model flags (`base_url`+`model` from the
  settings file + `NEO_API_KEY` env → verified fix, logs/config-e2e/).
  T3 (runtime): no router changes needed — `api_base` context already
  existed; T1 (harness): nothing to ingest. Other terminals' docs/scripts
  that reference `~/.neo/config.toml` keep working via the fallback; new
  docs should cite `neo config path`.
- 2026-09-13 (Terminal 2 session, branding round): **CLI interactive
  intent gate (Task E — a REAL defect fixed: `hi` used to launch a fix
  task) + splash/compact-header branding + oxblood theme. Additive;
  no boundary signatures changed; state.json schema unchanged.**
  (a) **Intent gate** — new module `cli/intent.py`:
  `classify(line) -> Intent(kind: "fix"|"convo"|"ambiguous", reply)`
  (deterministic, offline, never raises). `run_interactive` now
  consults it BEFORE `_run_one_fix`: conversational input (greetings,
  meta questions about neo, thanks, chit-chat) is answered inline
  with NO task launched; ambiguous input gets ONE clarifying
  question; only bug-shaped input reaches the harness loop. Slash
  commands, bare session commands (repo/model/help/exit), and
  custom-command templates (cli/commands.py) are dispatched BEFORE
  the gate and never classified — they are explicit by construction.
  Regression-pinned in tests/test_cli_neo3.py (21 conversational +
  10 bug-sentence + 4 ambiguous cases + 2 real-loop wiring tests).
  (b) **Branding** — `cli/ui.py`: theme accent moved amber→oxblood
  ramp (owner-selected after a measured contrast check: true oxblood
  ≤2.1:1 on black, illegible; accent #C9504C ~4.7:1, running
  #D98E5F ~6.6:1; the referenced NEO_DESIGN_SYSTEM.md does not
  exist — third documented occurrence). NEW presentation helpers
  `ui.print_splash` / `ui.print_compact_header` / `ui.wordmark_lines`
  / `ui.TAGLINE`: blocky NEO wordmark (6×26, █ with # ASCII
  fallback), splash shown ONCE per log root (`interactive.
  _is_first_launch` — no session index + no traced run dirs;
  `_code-graph`-style artifact dirs don't count), one-line compact
  header (◆ ember mark + version + model + repo) every regular
  session start. (c) **Spinner hardening** — NEW `ui.SPINNER`
  (encoding-probed: braille "dots" only when the console can render
  it, else ASCII "line"): `spinner="dots"` was hardcoded in
  `ui.status()` and `LiveMonitor.start()` and was a latent
  cp1252/legacy-console UnicodeEncodeError (probe-reproduced, same
  class as the GLYPS fix). (d) **`_execute_task` gained an optional
  trailing `state=None` kwarg** (session context: quiet flag today) —
  DEFAULT-NOOP for all existing callers; the two neo2 test stubs
  were updated. LiveMonitor's stop() summary line reshaped to the
  status-line grammar (events | calls | tokens | cost). Verified:
  test_cli_neo3 49/49; full CLI sweep 113/113; adversarial 62/62;
  parallel-session suites (cli_config + cli_plugins) 71/71; live
  3-session drive 30/30 checks (spinner frames + live labels +
  cost ticker proven ACTIVE during a real Docker-sandboxed run).
  Report kept at Temp/opencode/neo-branding-check/.
- 2026-09-13 (Plugins & Skills round): **NEW Skills system (Task A) +
  custom commands (Task B) + plugin bundles (Task C). All additive; NO
  boundary signature changes; state.json schema unchanged.** (a)
  **Skills** — new module `harness/skills.py`: a skill is a folder with
  a `SKILL.md` (frontmatter: name + description-of-when-it-applies;
  body: the instructions), discovered at `<repo>/.neo/skills/` (project,
  shared) + `~/.config/neo/skills/` (global) + `~/.config/neo/plugins/
  */skills/` (from installed plugins) + config `skills_roots`. Before
  planning, the harness scans descriptions, reads the full SKILL.md of
  plausibly-applicable ones, and injects them as a new planner-prompt
  section `## Applicable skills` — placed AFTER `## Retrieved context`
  and BEFORE `## Constraints` (the same after-the-cut discipline as
  decision memory/coordination: T3's difficulty predictor cuts at
  `## Retrieved context`, and the placement is runtime-side
  regression-tested). Matching is conservative keyword/camelCase-word
  overlap over issue + retrieval terms + repo path segments, with
  task-domain stopwords (fix/bug/test/python...) so generic words never
  trigger a skill. New config keys (harness/config.py): `skills_enabled`
  (True), `skills_max` (3), `skills_max_chars` (2500), `skills_roots`
  (None). New trace event: `skills` {matched, considered, skipped,
  error, section_chars}. Best-effort: a broken scan degrades to
  "(none matched)" + trace error, never a planning crash. (b) **Custom
  commands** — new module `cli/commands.py`: `.neo/commands/<name>.md`
  (project) or `~/.config/neo/commands/<name>.md` (global) or
  `~/.config/neo/plugins/*/commands/<name>.md`; `/name args` in the
  interactive session fills `$ARGUMENTS` and runs the template as a fix
  request. Built-in slash commands can never be shadowed. (c) **Plugins**
  — new module `cli/plugins.py`: a bundle directory (explicit
  `plugin.json` manifest: name/description/version/skills/commands/
  tools.verbs/mcp_servers, or an IMPLICIT layout — everything under
  skills/ + commands/ with the dir as name). Installed under
  `~/.config/neo/plugins/<name>/`. CLI: `neo plugin install <local path
  or git URL>` (git = depth-1 clone to temp + local install),
  `neo plugin list`, `neo plugin remove <name>`; PluginError → clean
  message + exit 2. **Tool extensions**: a manifest's `tools.verbs`
  extend the BATCH read-only allowlist via new
  `harness.tools.extend_batch_verbs(verbs)` (merged inside the
  non-capturing group by `_batch_readonly_pattern`); a FIRST-TOKEN
  deny set (rm/sed/python/git/curl/...) makes hostile or malformed
  verb entries structurally unable to widen the guard, and the
  forbidden-composition guard still applies to extended entries.
  **MCP references** are recorded + surfaced (consumption goes through
  the EXISTING `neo mcp call/list-tools` — a plugin points at servers,
  it never becomes one). A registry/marketplace stays out of scope per
  the original stretch-list decision. (d) **evals**: task set is now
  14 (new scenario `eval_skills_injection` — a genuinely-matching
  pytest-conventions skill via config skills_roots; guards loop
  machinery, determinism preserved on both arms) and the arm set 8
  (new `no_skills` arm; `skills_enabled` joined `_ROUND_KEYS`, so
  pre_round turns skills off too). Prior reports (13×7) remain
  comparable per-task; only the new task/arm have no history. New
  tests: tests/test_skills.py (27, incl. three REAL-loop e2e:
  content-receipt of a matching skill's marker in the planner's own
  user message, irrelevant-skill non-injection, OFF-arm never scans) +
  tests/test_cli_plugins.py (30, incl. plugin install→skill-discovery→
  planner-injection roundtrip, hostile-verb deny list, CLI
  install/list/remove roundtrip, git-clone failure containment).
  Full eval matrix 14×8: 112/112 CLEAN, 0 regressions (the standard
  pre-ship gate for the planner-prompt change). Example skills shipped
  at tests/fixtures/skills/ (pytest-conventions, django-conventions,
  pandas-vectorization) and the example plugin demonstrating all three
  pieces (skill + /review command + ruff tool verbs + MCP reference) at
  tests/fixtures/plugin-webapp-toolkit/. T3 (runtime): predictor cut
  marker untouched (regression-tested via the skills placement test +
  `_issue_text_from` invisibility test); new `skills` trace events are
  additive/safe to surface. T4 (memory): nothing to ingest; the
  `skills` trace event is dashboard-surfaceable like decision_memory.
- 2026-09-13 (Terminal 1, web-page reading round): **NEW step-session
  control signal `FETCH <url>` — general-purpose web-page reading
  (generalizes the Round-8 DOCS docs-lookup scope). All additive; NO
  boundary signature or state.json schema changes.** New harness module
  `harness/webfetch.py` (Terminal-1-internal surface: parse_fetch /
  fetch_webpage / fetch_and_render / extract_readable_text). A step
  session may output `FETCH <http(s) url>` in place of a bash command;
  the harness (core.run_step, raw + fence-stripped parse — never
  executed as shell) GETs the page, extracts readable text
  (readability-style, stdlib-only: script/style/nav/header/footer/
  aside dropped, innermost semantic container preferred), and
  re-injects it into the live session. **Scope/safety (read-only by
  construction)**: GET only — no forms/auth/POST possible; scheme
  allowlist + SSRF host blocklist (loopback/private/link-local/
  reserved) with per-hop redirect re-validation (the fetch runs on the
  HOST, outside the sandbox, so the guard lives harness-side); timeout
  (15s), response cap (1 MiB, enforced DURING read), rendered-text cap
  (3000 chars). Every fetch trace-logs a `web_fetch` event
  {step_id, turn, url, ok, status, chars} (URL + timestamp + outcome —
  same auditability as any tool call) and emits to the unified stream
  (shared.tracing, module "harness"). New config keys
  (harness/config.py): web_fetch_enabled (True), max_fetches_per_step
  (3), webfetch_timeout_s (15), webfetch_max_bytes (1 MiB),
  webfetch_max_chars (3000), webfetch_max_redirects (3). New trace
  events: web_fetch (additive, safe to surface). Eval harness:
  `web_fetch_enabled` added to `_ROUND_KEYS` + new `no_webfetch` arm +
  new scenario task `eval_fetch_webpage` (task set is now 13; changing
  it invalidates prior eval comparisons — noted in evals/AGENTS.md).
  T3 (runtime): planner prompt + difficulty predictor markers are
  UNTOUCHED (the FETCH doc block is in the STEP system prompt only);
  state.json schema unchanged. T4 (memory): nothing to ingest — the
  `web_fetch` trace event is dashboard-surfaceable like tool_call.
  Proof: fixture bug07_num2words (num2words NOT installed host-side —
  DOCS/pydoc genuinely miss; the `to="year"` kwarg lives only on the
  library's web page) fixed through the REAL loop + REAL Docker verify
  with the real PyPI fetch mid-task; honest control: the plausible
  wrong kwarg (year=True → TypeError) genuinely fails an attempt.
  tests/test_webfetch.py (38), full eval matrix 13×7 arms 91/91 CLEAN.
- 2026-09-12 (CLI session, PyPI packaging round): **Distribution name is
  `neo-harness` - the installed COMMAND stays `neo` (and the legacy
  `harness` alias). No code/contract changes; pyproject metadata +
  README install docs only.** Verified against PyPI's JSON API before
  building: `neo`, `neo-cli` (an AI CLI that itself installs a `neo`
  command - direct conflict), `neox`, and `pyvex` are all TAKEN;
  `neo-harness` (chosen in that round), `neo-agent-cli`, `neocli`, `neofix`,
  `neoai`, and `neo-code` are available. `python -m build` output
  `dist/neo_harness-0.1.0-py3-none-any.whl` + `.tar.gz` (145-file
  sdist, source packages only - no logs/fixtures swept in); wheel
  METADATA now carries readme/authors/urls/classifiers/keywords.
  Verified by CLEAN-VENV install (fresh venv, `pip install
  dist/neo_harness-0.1.0-py3-none-any.whl`): all 8 top-level packages
  import, `neo --help` / `--version` (0.1.0+source) / fix / status /
  memory / dashboard / mcp all resolve, and the offline demo entry
  point runs without a key. Publishing (twine upload) deliberately
  NOT attempted — needs the owner's PyPI account + API token.
  Install command: `pip install neo-agent-cli` → `neo`.
- 2026-09-12 (CLI session, interactive-mode verification): **One REAL
  bug found live + fixed in harness/editor.py::snapshot; no contract
  changes.** `cd <repo>; neo` (the no-args interactive flow) defaulted
  its log root to ./logs INSIDE the target repo; snapshotting the repo
  into logs/{task_id}/pristine then hit dst-inside-src and RecursionError
  (task "error" before the first model call). Every prior caller placed
  logs outside the repo, so the shape was untested. Fix: snapshot now
  excludes the dst parent-chain's top segment (e.g. `logs`) from the
  copy when dst lives under src — signature, Boundary 3/4 schemas, and
  all other callers unchanged (log-root-outside-repo behavior is
  byte-identical; a same-named `logs/` dir that is real repo content
  still copies). Regression-pinned both directions in
  tests/test_editor_prompts.py (4 new). The interactive flow itself
  re-verified live end-to-end through the real neo.exe (12/12 checks:
  banner, prompt, typed-sentence-as-issue, real loop, SUCCESS + diff +
  rationale, session index, never-mutate, rc 0) — details in
  cli/AGENTS.md "Interactive-mode verification round".
- 2026-09-12 (Terminal 1, Improvement Round 2 — agent-written tests):
  **`VerificationResult.structured_feedback` restored (additive, default
  `[]`).** The Round-8 delta was lost to the git-hook incident (the
  recovery note below delegated the re-write to T1); this round restored
  it exactly per the original design: an OPTIONAL `List[Dict]` of
  Boundary-7 FeedbackObject-shaped dicts (test_id, failure_type,
  summary, expected, actual, file, line, traceback_summary) filled by
  producers on failing runs; consumers treat absent/[] as "raw_output
  only" (graceful adoption — stub and historical serializations never
  carry it). `runtime/serialize.py` (T3's file, flagged here) round-
  trips the field with a default so older journals replay cleanly.
  Consumers: `_structured_or_tail` (harness/core.py) renders
  execution.feedback.format_objects when present, raw tail otherwise.
  ALSO this round (harness-internal, no boundary change): the
  agent-written edge-case test gate — after final verify passes, one
  model call writes edge-case tests (issue + diff in the prompt);
  sanitized filenames, compiled content, run through the SAME verify()
  in two stages (baseline rerun=0 on a pristine copy — a pre-passing
  generated test probes nothing and is dropped; post-fix with the final
  gate's flake-rerun + suite regression on a transient tree OUTSIDE
  work/). A post-fix failure poisons the attempt (the failing test is
  the next attempt's feedback); a GENERATION problem skips the gate
  (never overturn a verified fix over test-writing quality). Config:
  `agent_tests`, `agent_tests_max`, `agent_tests_dir`,
  `agent_tests_max_chars`. The self-critique gate (Agent Intelligence
  round) was also restored from the same incident (config keys
  `self_critique`, `self_critique_max_chars`; one review call of the
  diff vs the ORIGINAL issue; "no" poisons the attempt) — the sc8-*
  ablation logs referenced it; tests/test_self_critique.py passes again.
- 2026-09-11 (Observability & Eval round): **NEW cross-module tracing
  layer in shared/ — additive, no boundary changes.** `shared/tracing.py`
  (emit/emit_run/readers + safe_segment) gives every layer one
  normalized per-task event stream at `$NEO_TRACE_DIR/_trace/
  <task_id>.jsonl`; `shared/traceview.py` reconstructs a task's full
  lifecycle by merging that stream with the harness trace.jsonl, the
  worker journal, and the routing ledger. Contracts for emitters
  (never-raise, opt-in env, one-file-per-task, epoch ts) and the
  landed emitters per module are documented in shared/AGENTS.md.
  Consuming-module notes: execution/sandbox derives its task id from
  the mounted repo path (Win32 native backslash paths now trace — the
  original blanket rejection disabled execution tracing on Windows;
  tests/test_tracing.py pins both forms); runtime/model_router takes
  task_id from the existing router context; no emitter is required —
  absent NEO_TRACE_DIR the whole layer is a zero-overhead no-op, so
  existing tests/suites are unaffected. Also landed this round: the
  internal prompt-regression eval harness `python -m evals.run`
  (12 fixed tasks x 6 arms, real loop, exit 2 on regression; the
  standard pre-ship gate for prompt changes — evals/AGENTS.md) and a
  Win32 fix + eval-layout support in traceview's readers.
- 2026-09-10 (Terminal 3, Improvement Round 2): **Multi-candidate ensemble
  routing — additive mode, NO boundary changes.** New `runtime/ensemble.py`
  (task-level driver composing the existing scheduler/worker/harness
  machinery): predict task difficulty once from the issue text (same v2
  predictor + planner message shape the per-call router ingress scores —
  the hints are equal by construction and test-pinned), easy/medium →
  one sub-task with the ON arm's exact routing config, hard → TWO
  parallel cheap-pinned full candidate runs, escalate to ONE expensive
  run only if both miss. `runtime/model_router.py` and
  `runtime/difficulty.py` are byte-for-byte unchanged (single-attempt
  adaptive routing is NOT replaced — ensemble is a separately-ablated
  additional mode; three-arm comparison in runtime/AGENTS.md). The
  ablation runner grew `--arm ensemble` (+ a three-arm delta block +
  honesty notes in summary.json). New tests/test_ensemble.py (11,
  offline). Sub-task ids `ens-{a|c1|c2|x}-{slug}` share the run's
  logs root; per-bug target_test/test_command passthroughs ride the
  same keys the multirepo set uses. All surfaces runtime-internal.
- 2026-09-10 (Terminal 1 + Terminal 4, joint round): **Memory-informed
  planning — the planner now actively queries decision memory before
  planning, plus the ablation that measured it.** Contract changes:
  (a) **Boundary 4 ADDITIVE key**: state.json now also carries
  `repo_path` (written after the six schema keys, only when non-empty).
  Terminal 4's `ingest_state_file` has read `data.get("repo_path")` since
  Round 1 — the reader predated the writer; ingestion now stamps rows so
  repo-scoped queries work. Consumers must keep treating extra keys as
  ignorable (the six-key prefix order is unchanged and still asserted by
  tests). (b) **Terminal 4 surface extensions** (additive, defaults
  preserved): `DecisionStore.search(query, limit, repo_path=None)` —
  optional repo scoping, compared via normcase+resolve so relative and
  absolute forms of the same repo match; `memory.decision_store.
  open_default_store()` — the single opener all consumers should use
  (resolves memory.paths.decisions_db_path, i.e. HARNESS_DECISIONS_DB
  env-overridable — the ablation pins it per-run). (c) **New harness->
  memory consumption** (programmatic, per the Boundary 5 note above):
  before the planner call, `run_task` queries the store for decisions
  recorded against THIS repo and injects them as a new planner-prompt
  section `## Relevant past decisions`, placed AFTER `## Retrieved
  context` and BEFORE `## Constraints` — deliberately after Terminal 3's
  predictor cut marker so decision memory never shifts difficulty
  scoring (regression-tested from the runtime side of the contract in
  tests/test_decision_memory_planning.py). New harness config keys
  (defaults in harness/config.py): `plan_with_memory` (True; False = the
  ablation OFF arm, exactly one code path), `memory_query_limit` (6),
  `memory_max_chars` (1500). New trace event: `decision_memory`
  {query, matched, error, section_chars} (or {skipped} when off). All
  best-effort: missing module/broken store degrades to "(none recorded
  yet)" + error in the trace, never a crash. New module
  harness/decision_memory.py; tests/tests file:
  tests/test_decision_memory_planning.py (17).
  **Ablation (Task B, honest result)**: 5 fixture tasks where relevant
  prior decisions were seeded (genuinely mined from the v4/v3 ablation
  traces — the bare-pytest ImportError class that burned real turns
  before, sed-quote breakage, per-repo suite-invocation conventions —
  recorded via DecisionStore.record with repo_path, the documented
  path for facts not in state files; dedicated DB, production store
  untouched). Both arms real stack, same pinned model (z-ai/glm-5.3-free,
  adaptive routing OFF — no routing confound), arms differ only in
  plan_with_memory. Result: 100% success, 1 attempt, 0 verify failures
  BOTH arms — memory did NOT change task-solving power on this set. It
  changed efficiency: harness-level model calls 35->27 (-23%), tokens
  147,916->112,390 (-24%), proxy cost $0.2148->$0.1782 (-17%), and
  past-mistake recurrences 3->0 (three OFF tasks re-tripped the
  documented bare-pytest ImportError class; zero ON tasks did, and
  ON-arm commands visibly mirror the seeded conventions — python
  rewrite over chained sed, python -m pytest from repo root). Honest
  caveats: n=5x1rep directional only; each arm absorbed one symmetric
  mid-run crash-retry; the ledger's raw call counts include router-
  internal empty-response retries (the corrected trace-level counts
  are what's quoted); a parallel terminal's in-flight agent-tests
  feature ran symmetrically in both arms. Driver:
  runtime/ablation_memplan.py; full data + post-run analysis:
  logs/memplan-ablations/run1/summary.json.
- 2026-09-10 (Terminal 1, Round 8): **Agent execution layer & tooling —
  structured tool errors, BATCH, lint gate, DOCS lookup. All additive;
  no boundary signature or state.json schema changes.** New harness
  module surfaces (Terminal-1-internal, no cross-module contract
  changes): harness/tool_errors.py, harness/lint.py,
  harness/docs_lookup.py. (a) **Structured tool errors (Task A)**:
  every failing tool result is classified into a stable error kind —
  model-facing `TOOL ERROR [<kind>]: <detail>` + suggested fix, 10
  classes (file_not_found, command_not_found, malformed_patch,
  syntax_error, import_error, undefined_name, permission_denied,
  timeout, argument_error, command_rejected) + internal_error fallback;
  classify() never raises; exit=0 output format UNCHANGED.
  (b) **BATCH protocol (Task B)**: a step session may issue
  `BATCH <read-only cmd> ;;; <cmd>` — strict verb allowlist + forbidden
  composition chars, all-or-nothing validation, executed concurrently
  (ThreadPool 4) via throwaway sessions; measured 1.97× on an 8-op read
  phase (real Docker). Deliberately NOT generalized: read-only,
  order-independent only. (c) **Lint gate (Task C)**: stdlib AST pass
  (syntax + module-level undefined names, false-negative biased) at
  TWO points — SUBMIT-time in-session feedback, and a pre-final-verify
  short-circuit that retries WITHOUT burning a verify cycle on an edit
  the AST pass knows is broken. Never gates success (verifier-gated
  completion unchanged). New config keys: lint_gate (True), lint_names
  (True). (d) **DOCS protocol (Task D)**: `DOCS <target>` resolves
  library/API docs cache → pydoc (subprocess) → opt-in PyPI (gated OFF
  by default); read-only; shared cache at logs/_docs-cache/ (harness-
  owned, outside any repo — same convention as _code-graph/; harmless
  to prune). New config keys: docs_lookup_enabled (True),
  docs_lookup_allow_remote (False), max_docs_per_step (3),
  docs_max_chars (3000). New additive trace events (safe to surface):
  batch_call, batch_rejected, tool_error, lint_failed, docs_lookup.
  New tests: tests/test_tool_errors.py (26) + tests/test_batch_docs_
  lint.py (28), both Docker-free; full module selection 204 green.
  NOTE for all terminals: a parallel `git reset --hard` at ~09:37
  destroyed uncommitted harness work tree-wide (Round-6 hardening was
  recovered from dangling stash fa75dcb; my Round-8 wiring re-applied;
  the state-machine/self-critique session re-landed their own) — commit
  or back up outside the tree after every green milestone.
- 2026-09-10 (Terminal 1, Improvement Round 2): **Coordinated
  multi-file changes — detection + atomic group validation/rollback.**
  (a) **Boundary 4 ADDITIVE key `change_groups`** (schema updated
  above): planner steps may now carry `"change_group": "<name>"`; the
  harness unions each group's files_hint into
  `state.json["change_groups"] = {name: [files]}` — written after
  repo_path, absent when the plan declares none, hydrated on resume,
  malformed-value-tolerant. Consumers MUST treat it as ignorable per
  the additive-key rule (same standing as repo_path). (b) **New module
  harness/coordination.py** (Terminal-1-internal; consumes
  memory.code_graph's raw Graph like retrieval does — no new
  memory-side surface): `detect_coordinated_change(repo_path,
  issue_text, changed_files, plan_texts?, index_root?, kind?,
  max_files?, protected_patterns?) -> dict` — structural fan-out via
  CALL edges (callers of symbols defined in changed files) + IMPORT
  edges (importers of changed modules), classified by issue vocabulary
  ("signature" / "rename"); never raises, degrades to
  detected=False without the graph. Protected paths (tests/*, VCS)
  are EXCLUDED from suggested groups (`excluded` key) — an atomic
  agent-edit group can never include a forbidden path. (c) **Plan
  schema extension (planner-facing, additive)**: the optional
  change_group field per step; the planner prompt gains a
  `## Coordinated-change fan-out` section (AFTER `## Retrieved
  context` — T3's difficulty predictor cut marker is unchanged) and a
  change_group schema line in PLANNER_SYSTEM. (d) **Atomicity
  semantics in run_task**: only plan-DECLARED groups are enforced (the
  detection is advisory — it shapes the prompt; the declaration is
  the commitment). Pre-final-verify gate: a group with some-but-not-
  all members changed poisons the attempt (feedback names the missing
  files) and rolls the GROUP back together via new
  `editor.restore_group` (config `coordination_rollback`:
  "group" default | "all" | "none"); a FAILED verified attempt also
  rolls touched groups back together. New trace events:
  coordination, change_groups, coordination_gate_rejected,
  coordination_rollback — safe to surface in dashboards. New config
  keys (harness/config.py): coordination_detect, coordination_gate,
  coordination_min_files, coordination_rollback. Tests:
  tests/test_coordination.py (31 unit) +
  tests/test_coordination_e2e.py (5 e2e, Docker-gated — a GENUINE
  4-file scenario, fixture tests/fixtures/bug06_coord: method rename +
  flag removal rippling model -> serializers -> reports -> api), both
  added to harness-ci.yml. Full 265-test harness selection green.
- 2026-09-10 (DX round, root tooling): **Dev-experience tooling landed —
  NO boundary signature changes anywhere.** (A) `scripts/dev-setup.sh`
  (+ `make dev`): one-command setup — deps (`pip install -e ".[dev]"`,
  auto-venv on PEP-668 hosts), pre-commit hook install (preserves a
  user hook as `.user-*`), Docker verification, CLI smoke. Idempotent.
  (B) **The repo now has ONE canonical linter: ruff** (`[tool.ruff]` in
  pyproject.toml). The pre-commit hook (`scripts/hooks/pre-commit`),
  `make lint`, and CI all use it. RULE FOR ALL TERMINALS: any future
  lint surface — including an in-sandbox agent lint tool — must REUSE
  this config, never add a second linter. Pre-linter debt is capped per
  file by a RATCHET (`scripts/lint_ratchet.py` +
  `scripts/lint-baseline.txt`): new files must be violation-free;
  baselined files may not gain debt; `--update-baseline` is a
  deliberate action. (C) `cli/errors.py`: every CLI failure surface
  (fix/run-benchmark/interactive/resume/top-level net/`python -m cli`
  import guard) now prints a plain-language cause + "check:" lines;
  raw tracebacks are saved to a file, never displayed. Exit-code
  contract (0/1/2 + 130) unchanged. (D) **Operational incident +
  recovery, all terminals read this**: a `git reset --hard` during
  hook testing destroyed the tree's uncommitted Round-8 tracked-file
  deltas. The neo-era working tree was recovered from
  stash tag `recovery-stash-round7-sim2` (20 files, verified
  identical to the pre-incident disk state); T1's 03:23 state-machine
  core/config/prompts snapshot is preserved under tag
  `recovery-stash-t1-agent-intel` (their in-flight core.py re-writes
  were observed landing after, so the tag is a fallback, not a
  restore source); harness/retrieval.py (size_context_budget /
  rerank_files), shared/types.py (structured_feedback) and
  execution/verify.py (Boundary-7 feedback fill) Round-8 deltas were
  NOT recoverable from git — T1/T2 please re-write those from your
  session context. Untracked Round-8 files all survived untouched.
- 2026-09-09 (Terminal 1, Round 6): **Opt-in model completion-token budget
  (ctx key "max_completion_tokens") + adversarial-hardening notes.** Two
  runtime-owned changes landed during the Round-6 multi-repo validation, both
  ADDITIVE and opt-in (absent key = previous behavior; no boundary signature
  changed anywhere): (1) `runtime/worker.py` passes
  `cfg.get("max_completion_tokens")` into `set_call_context`, and
  `runtime/model_router.call_model` forwards it as litellm `max_tokens`
  when set. Motivation: the tokenrouter endpoint began burning an unbounded
  token budget on hidden reasoning_content (content=None +
  finish_reason=length, twice on the R6 planner prompts at 4000-token
  budgets, 1453s/265s); an explicit budget guarantees room for the visible
  answer. (2) T3's test-encoded intent that AuthenticationError is NOT
  retried was deliberately PRESERVED (T1 probed adding a retry, found the
  module's own test forbidding it, and reverted — the observed gateway auth
  flake remains a documented endpoint-health note, not a code change).
  Separately, harness-owned adversarial hardening (tests/test_adversarial.py,
  43 tests): `editor.is_protected` now '..'-normalizes paths and treats
  .git/.hg/.svn as ALWAYS protected; `editor.check_edits` scans work/ for
  agent-forged VCS paths (changed_files deliberately skips them, so a
  diff-only check never saw the class); `tools._DENY_PAT` covers reordered
  rm flags ('rm -fr /') and pipes into bash/zsh/dash from curl AND wget.
  CRITICAL harness fix found by the e2e prompt-injection test:
  `core.run_task` now re-runs `check_edits` before final verify — a
  protected-path violation used to poison only the STEP, and a defused test
  + real fix could still mint a verifier-gated SUCCESS (real success-path
  bypass, reproduced, fixed, regression-tested).
- 2026-09-09 (Terminal 2, Neo CLI Side Task 2): **CLI session persistence
  + slash commands + config file + plan preview.** Four additions, all
  leaning on existing backend contracts (no boundary changes; exit-code
  contract 0/1/2+130 intact). (A) `neo --continue` /
  `--resume <task_id>` / `--list-sessions`: a session index
  (logs/.neo-sessions.jsonl, recorded on completion AND interrupt) plus a
  directory-scan fallback discovers resumable runs (resumable = state.json
  completed AND remaining steps AND no final result event — exactly the
  harness resume contract's precondition; pre-step-1 interrupts correctly
  NOT resumable, by the documented fresh-restart semantics). `_resume_task`
  rebuilds the Task from the run's own task_start trace event (the public
  observability surface) with config["resume"]=True — LIVE-verified:
  child hard-killed after step 1 → dir-scan flags resumable → resume
  skips the completed step, continues the in-flight attempt, finishes,
  git output + rationale produced. (B) Interactive slash commands
  /status /diff /sessions /resume /approve /reject /cancel /quiet /help —
  /approve//reject write decision.json via runtime.approval's EXISTING
  file protocol; /cancel is SIGINT semantics (checkpoints kept). (C) NEW
  cli/neoconfig.py — ~/.neo/config.toml (or $NEO_CONFIG): model, provider,
  budget_cap_usd, max_retries, plan_preview, log_verbosity, log_root;
  precedence explicit-flags > file > DEFAULTS; broken TOML warns once and
  is ignored, unknown keys pass through. (D) plan_preview (config key,
  default OFF = autonomous): renders the FIRST plan trace event (numbered
  steps + checkpoints) and prompts before the edit phase; reject cancels
  with checkpoints kept; reuses the Ctrl+C machinery, NOT a new protocol —
  the worker-level final-diff approval gate is unchanged and separate.
  NEW tests/test_cli_neo2.py (24); full CLI regression 126/126.
- 2026-09-09 (Terminal 2, Neo CLI side-task): **PROJECT RENAMED Neo —
  Boundary 6 entry point is now `neo`; interactive natural-language mode
  is the primary UX.** Coordinated with Terminal 4's Round-6 state (their
  adversarial hardening is complete and untouched: 62/62 green; only 3
  decorative output assertions in tests/test_cli.py updated to the new
  rich-render format, same intent — flagged for T4's review). Changes:
  (a) pyproject name=neo, console script `neo = "cli.main:main"`,
  `harness` kept as a working alias until docs migrate (demo/ still says
  `harness fix` — T4's module, deliberately untouched); argparse
  prog="neo". (b) NEW cli/ui.py — shared rich Console + NEO_THEME
  (amber/ember palette) applied to every command; ui.GLYPHS with
  encoding-probed ASCII fallbacks. (c) NEW cli/interactive.py — no-args
  `neo` on a TTY = plain-language session (sentence becomes the issue
  text, repo from CWD, repo/model switching, status/diff re-render);
  non-TTY no-args = usage error, never a hang. LiveMonitor tails
  trace.jsonl (the public observability surface — no run_task internals
  reached) for a live spinner + accruing cost during runs; benchmark live
  table; approval prompts render the request.json diff and write
  decision.json; Ctrl+C sweeps this process's orphaned containers via
  execution.sandbox's public reaper. (d) **Two real cross-platform bugs
  found + fixed by verification, not assumption**: `pip install -e .` was
  broken for everyone (flat-layout package discovery — explicit package
  list added; `neo.exe`/`harness.exe` now install and run on Windows,
  same flow Linux/macOS), and legacy cp1252 consoles crashed on the first
  glyph (UnicodeEncodeError in rich's LegacyWindowsTerm — GLYPHS
  fallbacks). (e) NEW tests/test_cli_neo.py (10: theme roles, no-color
  strip, diff classification, monitor tracking/rotation-survival,
  non-TTY dispatch subprocess, scripted interactive fix, interrupt sweep
  up/down). 109 CLI-adjacent tests green; rich>=13.0 added to
  dependencies. Exit-code contract 0/1/2 + 130 (interrupt) unchanged;
  no other contract surface changed.
- 2026-09-09 (Terminal 4, Round 6): **ADVERSARIAL HARDENING — one REAL
  data leak found + fixed (task-id path traversal), plus two CLI input-
  validation crashes; NEW shared surface in memory/paths.py.** No
  signature changes anywhere.
  (a) **The leak (live-confirmed before fixing)**: `mcp_server.
  task_status(task_id)` and `harness status --task-id <id>` built
  `logs/<task_id>/state.json` by naive path join — on Windows,
  `Path('logs') / 'C:/evil'` DISCARDS the base (drive-lettered
  operands) and `..`-chains resolve through, so a hostile task id
  read ANY state.json-shaped file on the host (reproduced against
  real production task data). Fix: both surfaces now resolve ids
  ONLY through the new shared guard `memory.paths.safe_task_dir/
  is_safe_task_id` — semantic rejection (separators, null bytes,
  `:`-drive forms, Win32 `' ..'`-style whitespace/dot tricks) +
  resolve-containment under the logs root; interior spaces/unicode
  stay allowed. If YOUR surface joins a task id onto a path, use
  this guard — do not re-derive.
  (b) CLI input validation: malformed subset entries (`{"repo": 123}`
  and `"config": "string"`) crashed with tracebacks; a null-byte
  `repo` spawned doomed scheduler workers (found live). All rejected
  at `_load_subset` validation time now; `--issue @file` handles
  binary/null-byte paths; exit-code contract (0/1/2) unchanged.
  (c) Verified-contained surfaces (no changes needed): query_
  decisions SQL injection (parameterized + literal-keyword ranking),
  secret harvesting (production DB swept: 0 secret-pattern hits),
  CodeGraph hostile queries (indexed-path misses only; content never
  served), MCP SDK exception containment (generic `Error executing
  tool` — no traceback leaks), CLI shell-injection payloads
  (`--repo`/`--issue` are data; zero shell=True/os.system/eval in
  the repo; marker-file side effects checked: none).
  (d) Tests: tests/test_mcp_adversarial.py (39) +
  tests/test_cli_adversarial.py (62), incl. the exploit replayed over
  the REAL stdio transport and a real `python -m cli` subprocess.
  Production audit artifacts: logs/mcp-adversarial/ (19/19) +
  logs/cli-adversarial/ (38/38). Full per-probe outcome tables:
  mcp_server/AGENTS.md, cli/AGENTS.md, memory/AGENTS.md (Round 6).
- 2026-09-09 (Terminal 2, Round 6): **ADVERSARIAL SECURITY TESTING —
  sandbox CONFIRMED under deliberate attack; no contract changes, no
  production-code changes.** New `execution/sandbox_adversarial.py`
  (NOT in pytest — run explicitly, like sandbox_stress.py): Task A
  sequential (`python -m execution.sandbox_adversarial`) + Task B
  concurrent (`--concurrency N`). 24/24 sequential attacks HELD (escape:
  host mounts/FS/shadow/pidns/docker-socket/su/chown/proc-mount/
  workspace-siblings/network/env — all blocked; resources: fork bomb
  collapsed at pids-limit, 2GB mem bomb+leak OOM-killed (137), tmpfs dd
  ENOSPC'd at exactly the 256m cap, CPU ratio 3.7-6.2x under --cpus 1.0,
  infinite spin timeout-killed, 100MB output flood returned bounded).
  Cross-container: filesystem/network/bridge-scan isolation all held;
  victims undisturbed. Task B: 78 concurrent hostile runs at widths
  8/10/16 (16 simultaneous bombs on the 12-core VM) — 0 findings,
  canary tasks clean. ONE DESIGN FINDING recorded (not a bug): the RW
  bind mount lets a container write host disk unquota'd (no docker
  primitive for bind-mount quotas) — inherent to the RW-mount contract
  (T1 diffs host-side); see execution/AGENTS.md Round 6 for the full
  attack/evidence table. 9 permanent regressions added in
  tests/test_sandbox.py::TestSandboxAdversarial (module suite 84/84).
- 2026-09-09 (Terminal 2): **FLAKE FIXED — `test_concurrent_burst_no_leak`
  (the flag filed by Terminal 3, below — CLOSED).** T3's diagnosis was
  confirmed empirically before fixing (20 consecutive `docker images`
  capture pairs over the unchanged 25-image cache: 12/20 raw-list
  mismatches, 0/20 sorted; the cache holds an exact same-second pair from
  the Round-4 builds), and a live reproduced red exposed a SECOND,
  coexisting mechanism in the same test: the one-shot `docker ps -a`
  residue assertion catching a container still in daemon-async `--rm`
  teardown right after the 20-burst (the mechanism Terminal 2's own
  Round-4 note suspected). Fix is tests-only (no production code, NO
  contract change — the production module never had the ordering bug):
  (a) image captures now go through `_image_set()` (sorted/set
  comparison, T3's suggested fix) with growth bounded to the repo's own
  dep tag (`grew <= {tag}`, the sandbox_stress.py pattern); (b) all three
  one-shot residue assertions in tests/test_sandbox.py (this test,
  `test_no_container_left_behind`, `test_orphaned_container_reaped_by_
  peer`) now poll via `_assert_no_hexec_residue(timeout_s=15)`, scoped
  to THIS process's `hexec-p<pid>-*` containers — the scoping matters on
  this machine: parallel pytest suites share one Docker daemon, and a
  global `hexec-` check sees the other suite's LIVE containers (found
  live when two verification suites ran concurrently). Teardown lag
  can't false-red, real leaks still fail loudly, and the orphan-reap
  test now asserts the victim-gone invariant (a concurrent peer's
  opportunistic sweep reaping the victim first is the self-healing
  mechanism WORKING, not a failure). Two regression tests pin the fix
  under the trigger conditions themselves:
  `test_regression_image_list_ordering_immunity` (8 back-to-back
  captures, every pair must match — the ordering hazard, formerly 12/20
  raw mismatch rate) and `test_regression_burst_back_to_back_with_
  other_tests` (predecessor run + immediate 20-burst, the exact
  order-dependent regime that historically went red). Verified: module
  suite 75/75 in order; 4 consecutive trigger-order integration-class
  runs green; 2 randomized-order full-suite runs green (pytest-randomly,
  distinct seeds); two integration suites run CONCURRENTLY against the
  shared daemon both green — the regime that false-reded the first
  iteration of this fix. Real Docker throughout. No other module needs
  to act — this was test mechanics, not behavior.
- 2026-09-09 (Terminal 1): **Round-5 closeout — spec item 13 (reversible
  compaction) finished for real + T3's state.json flag fixed.**
  (a) **RECALL protocol (on-demand reinjection, item 13)**: a step session
  may output `RECALL <terms>` in place of a bash command. The harness
  (core.run_step) intercepts it — on both the raw reply and its
  fence-stripped form, so a fenced RECALL is never executed as shell —
  greps THIS task's trace.jsonl via the new `TraceLogger.find_events(query,
  kinds=None, limit=5, max_chars=4000)` (case-insensitive substring over
  kind+data, most-recent-N, per-entry char cap, malformed lines skipped,
  never raises), and re-injects the matching entries into the live
  session's context as a user message ("RECALL results for '<query>'").
  state.json remains the compacted view; trace.jsonl the full-fidelity
  store; RECALL is the retrieval hook between them — exactly what item 13
  meant by "reversible." Budgets (all task.config, defaults in
  harness/config.py): `max_recalls_per_step` 3, `recall_results_cap` 5,
  `recall_max_chars` 4000; budget exhaustion nudges back to bash, never
  deadlocks. New trace event: `recall` {step_id, turn, query, matched}.
  Consumers: T4's dashboard may surface `recall` events (a step that
  pulls old detail back is interesting signal); T3's difficulty
  estimator keys on PLANNER prompt markers (`## Issue` / `## Retrieved
  context`) — those are UNCHANGED; the RECALL doc block was added to the
  STEP system prompt only, so your predictor's markers are intact.
  (b) **T3's 2026-09-09 flag (cli-real-smoke state.json: success with
  completed_steps: []) — CLOSED.** Root cause (from that run's trace):
  the step's commands applied the fix but the turn budget exhausted
  before SUBMIT (ok=False "exhausted ... turns"), final verify then
  passed and the task succeeded — with complete_step never called. Fix:
  on a VERIFIED success, the harness now records every plan step that
  RAN in the winning attempt as completed (new
  `TaskState.complete_all_ran_steps(steps)`; the verified diff subsumes
  each ran step's work). state.json schema unchanged; `harness status`
  now renders true progress on that path. Regression-tested
  (exhausted-turns success → completed_steps == plan, remaining empty).
  (c) Test status: 107/107 harness tests green (78 prior + 26 RECALL
  unit + 3 new e2e: cross-session RECALL reinjection proven
  content-receipt-wise, RECALL budget-exhaustion, exhausted-turns
  success-state).
- 2026-09-09 (Terminal 2 flag, filed by Terminal 3):
  `tests/test_sandbox.py::TestSandboxIntegration::
  test_concurrent_burst_no_leak` went intermittently red after Round-4's
  real-model runs (ablation v4 + CLI real smoke built ~13 new dep images
  in rapid succession). NOT a container leak — the failing assertion is
  the before/after `docker images` LIST comparison: several images now
  share a creation second, and `docker images` orders same-second images
  nondeterministically, so two consecutive invocations can differ at
  those indices. Suggested fix (your module, your call): compare SETS
  (sorted) instead of lists, or filter to the test repo's own tag.
  Verified green before tonight's runs; ~1-in-2 flaky now, purely from
  image-list ordering.
- 2026-09-09 (Terminal 3): **Round-4 continuation — Task B/C closure.**
  (a) **Ablation v4 re-run** (`logs/ablations/v4/`, both arms in one
  invocation, 16/16 tasks green in BOTH arms — fixture-path fix
  confirmed): OFF 100%/$0.1505 vs ON 100%/$0.0581 (2.59× cheaper,
  3.3× faster wall; 68 cheap + 3 expensive calls; 1 genuine
  struggle escalation, 1-of-2 scary-text false escalation). Together
  with `v3-expanded-fixed` (3.47×) this makes the Phase-5 result
  REPRODUCED twice in independent runs. (b) **Ablation summary-merge
  fix**: separate `--arm` invocations sharing one `--out` now MERGE
  into summary.json (arms accumulate + `arm_runs` provenance) instead
  of clobbering — the Round-3 gotcha is fixed; running both arms in
  one invocation remains the default. (c) **FAKE-path split-brain
  FIXED (runtime-owned)**: the fake harness's state.json default moved
  from repo-CWD `./logs/{task_id}` to the PINNED log_root (both
  writer and reader now resolve through `runtime.paths.
  state_json_path`) — `harness run-benchmark --log-root <custom>` +
  fake harness previously broke resume outside the repo root.
  `fake_state_dir` config still overrides (all existing tests
  unchanged). Verified through the CLI: 3-task fake subset @ custom
  root with a mid-run kill → resume=True, all artifacts under the
  custom root, zero leakage into ./logs. (d) **REAL-harness path
  through the CLI verified**: one smoke_repo task, real model,
  adaptive routing ON, `hang_heartbeat_stale_s: 300` → success,
  $0.0081, all artifacts under the custom root. Honest note: the
  first worker was hang-killed at exactly 300s (state_stale) because
  the cheap tier's first planner call ran >300s under load, then the
  requeued relaunch finished — T4's 300s guidance is BORDERLINE when
  the cheap tier is loaded; 600+ is safer (the ablation uses 1200).
  (e) **New config key (test/offline only): `mock_script`** —
  plan+per-step bash scripts from a plain dict, consumed by
  runtime.worker via `runtime.mock_provider.install_script` so the
  REAL harness loop runs deterministically in subprocess workers
  with zero network (used by `runtime.stress --mode real`).
- 2026-09-09 (Terminal 4): **MCP client + `harness mcp` CLI surface**
  (spec item 30, "consume external MCP tools"): new
  `memory/mcp_client.py` — minimal stdio MCP CLIENT using the same
  official SDK (`list_mcp_tools(server, cwd?, env?)`,
  `call_mcp_tool(server, tool, args?, cwd?, env?)`; results are
  plain dicts {"ok", "text"/"tools", "error"}, never raises). CLI:
  `harness mcp list-tools "<server cmd>"` and `harness mcp call
  "<server cmd>" <tool> [--args '{...}']` — the demoable
  "consume an external MCP server" path; our own mcp_server doubles
  as the test target (12 tests, tests/test_mcp_client.py: real
  subprocess round-trips incl. env passthrough, quoted-Windows-path
  argv handling, bad-server/bad-tool → ok=False, never a traceback).
- 2026-09-09 (Terminal 1 flag, filed by Terminal 3): in the Round-4
  CLI real-run (`logs/cli-real-smoke/`), the final state.json of the
  successful task shows `completed_steps: []` + `remaining_plan:
  [step]` while trace/state agree the task finished (result:
  success, decision recorded). The task was hang-killed pre-step and
  relaunched fresh, so this looks like a resume/restart path in
  harness.core not re-marking the step complete on the final write.
  Cosmetic (progress authority disagrees with result), but
  `harness status` renders "0/1 complete" for a successful task.
  Terminal 1: please check the final state write on the
  killed-then-restarted path.
- 2026-09-09 (Terminal 1): **Round-4 report — first full-stack DoD on an
  unfamiliar real OSS repo (jaraco/path) + a harness bug found live and
  fixed.** (a) **Self-audit of spec items 1-17: COMPLETE, all CORE items
  implemented** — full per-item verdicts in harness/AGENTS.md Round 4.
  Honest weak spots documented (item 13 has no on-demand reinjection of
  older detail — trace.jsonl keeps everything, state.json is the compacted
  view; failure classification remains Phase-3+ work at the documented
  `last_feedback` seam). (b) **harness/editor.py behavior change
  (internal semantics, no signature changes)**: `changed_files()` now
  EXCLUDES verifier-created run artifacts (`.coverage`/`.coverage.*`,
  `.hypothesis`, `.cache` dirs, plus the pre-existing skip set) and
  `unified_diff()` treats a non-UTF-8/binary changed file as binary
  (returns None) instead of raising UnicodeDecodeError. Why: a real OSS
  run crashed AFTER final verify passed because the repo's pytest
  materialized a binary SQLite `.coverage` in work/ — the strict decode
  was outside its guard. A verified fix must never die over presentation.
  Consumers: T4's dashboards / status readers see cleaner files_touched
  (no `.coverage` entries); T2's git_output receives no artifact files.
  (c) **OSS-run proof (first non-fixture end-to-end)**: real Scheduler →
  worker subprocess → real harness → real cloud model (6 calls, $0.053)
  → real Docker sandbox → verifier-gated success in 1 attempt, WITH
  approval gate (request/decision file protocol), git branch+commit+PR
  description, rationale.md, and T4 memory ingestion confirmed via the
  MCP query_decisions surface. First attempt failed honestly (exposed
  (b)); artifacts under logs/oss-round4/.
- 2026-09-08 (Terminal 3): **Round-4 report.** (a) **T1's approval/hang
  finding FIXED** (their entry below): the runtime checkpoint
  (`logs/{task_id}.runtime/checkpoint.json`) now carries an
  `awaiting_approval` boolean — the worker sets it before parking in the
  approval gate and clears it ATOMICALLY with `status: "finished"` in
  the final checkpoint write (no separate finally-clear: that reopened a
  kill-during-teardown race, found live). The scheduler's state-stale
  hang kill SKIPS workers whose checkpoint has `awaiting_approval: true`
  (or `status: "finished"`) while their heartbeat is fresh; a dead
  heartbeat and the wall-clock cap still kill. Net effect: T1's "pin
  hang_heartbeat_stale_s >= approval_timeout_s" mitigation is no longer
  required for gate-parked tasks. Verified: unit tests (2 new live
  scheduler tests) + 45 overlapping real-harness parks, each ~30s past
  the stale window, 0 gate-parked kills. (b) **Stress re-verified at
  scale against the fuller harness** (T1's git output + rationale in the
  loop): real 45@cap45 w/ 8 kills — 45/45 tasks produced git.json +
  rationale.md + trace events while being killed/resumed; new
  stress-mode `--approval` reproduces the gate scenario at scale.
  (c) **Ablation final data** (Phase 5): 16-task paired run,
  `logs/ablations/v3-expanded-fixed` — OFF 100%/$0.185 vs ON 94%/$0.053
  (3.47x cheaper; 57 cheap + 2 expensive calls; single failure was a
  gateway flake, not routing). The earlier `v3-expanded` runs are
  INVALID (runner passed bare fixture dir names — 5 tasks errored at
  snapshot in both arms); fixed + re-run. Full write-up in
  runtime/AGENTS.md Round 4. (d) Measured for everyone's benefit: healthy
  real-harness work phases run up to ~98s between state.json writes
  under 45-way Docker load (p95 75s) — size hang_heartbeat_stale_s
  above REAL WORK gaps, not just model-call latency.
- 2026-09-08 (Terminal 1): **Round-3 report.** (a) **context.py repair
  (Terminal 4's 2026-09-08 entry) reviewed — CLOSED**: the deletion was
  correct; it matched my intended shape (ONE `_write`; `save_plan_steps`
  writes only plan.json). Regression tests added in
  tests/test_config_trace_state.py (import/AST parse of every harness
  module + single-`_write` structural invariant); verified they catch the
  original corruption by re-introducing it. (b) **New harness outputs on
  verified success (spec items 26/29), no contract changes to TaskResult**:
  `run_task` now writes `logs/{task_id}/rationale.md` (via
  execution.rationale) and `logs/{task_id}/git.json` (via
  execution.git_output — real branch + commit in the harness's PRIVATE
  work/ copy; original repo untouched) + `rationale`/`git_output` trace
  events. Both best-effort: failures degrade to trace events, never
  change the verifier-gated outcome. New task.config keys (harness-owned,
  documented in harness/config.py): `git_output` (bool, default True),
  `rationale_log` (bool, default True), `branch_name` (str, optional).
  Terminal 4: `harness status`/dashboard may surface rationale.md and
  git.json — they're plain files under logs/{task_id}/. (c) **New
  test-only cross-process hook**: env var `HARNESS_SCRIPTED_MODEL=<json
  spec>` makes harness.deps resolve a deterministic scripted model
  (harness/_stubs/scripted_model.py) inside SUBPROCESS workers where
  set_call_model injection can't reach. Never use in production. (d)
  **Approval-mode wiring CONFIRMED LIVE** end-to-end (scheduler → worker
  subprocess → real harness → request.json/decision.json with an external
  approver; both approve and reject paths) — see harness/AGENTS.md Round
  3. **Finding for Terminal 3**: a worker blocked in the approval gate
  stops touching state.json, so the scheduler's hang check (default
  `hang_heartbeat_stale_s: 30`) kills it mid-gate unless configs pin
  `hang_heartbeat_stale_s` >= `approval_timeout_s` (same mitigation as
  your long-model-call note). Suggested runtime-side fix: keep the
  heartbeat daemon beating while parked in the gate.
- 2026-09-08 (Terminal 2): Task-A integration spec for T1's run_task
  wiring of git_output/rationale is written out IN FULL in
  execution/AGENTS.md ("Task A (Round 3)" section): exact signatures,
  call order (build_rationale FIRST, then produce_git_output on the
  success path where core.py does unified_diff today, ~core.py:454-460),
  argument semantics (pristine_dir = logs/{task_id}/pristine is
  STRONGLY recommended — makes the fix commit's diff exactly the fix),
  return shape, error mode (GitOutputError → log + success-with-null,
  a verified fix shouldn't die over presentation), and the trace key
  rationale depends on (baseline_verify.data.raw — keep the last-3000-
  chars shape). No contract changes: signatures are exactly what
  Boundary-1-adjacent modules documented in the 2026-09-07 entry.
- 2026-09-08 (Terminal 2): CONCURRENCY HARDENING of the live sandbox
  (Round 3, found + fixed under real 40-50-concurrent-task load):
  (a) NEW PUBLIC FUNCTION `execution.sandbox.reap_orphaned_containers(
  include_stale_names=False, dry_run=False) -> list[str]` — kills
  hexec-* containers whose owning host process is dead. Container
  NAMES now embed the owner PID: `hexec-p<pid>-<uuid>` (the `hexec-`
  prefix you may already be matching still works; old-format names are
  never reaped). execute_sandboxed self-heals: every call runs this
  sweep rate-limited (≤1/30s/process), so hard-killed scheduler workers'
  containers get cleaned by surviving peers within ~35s instead of
  running to full command duration (measured). Terminal 3: your
  proc.kill()-based crash kills are now leak-free against the Docker
  sandbox — no scheduler-side change needed. (b) Image builds are
  serialized across PROCESSES via a temp-dir lockfile (one builder,
  peers wait on the image cache; dead-builder bail-out) — safe for N
  scheduler workers racing one cold dep image. (c) Verified at scale:
  new `execution/sandbox_stress.py` (NOT in pytest; spawns 50 real
  container-driving children with mid-run kills + requeue respawns):
  50@50 w/ 7 kills and 50@cap30-shape w/ 20 kills — ALL checks pass,
  zero container residue, bounded image growth. Full stack re-verified:
  71 T2 + 28 T1-e2e + 12 T3-scheduler tests green post-change.
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
- 2026-09-08 (Terminal 2): verify() flake semantics BUG FIX (Round-4
  self-audit) — the documented "a timeout counts as a distinct outcome"
  (see entry of 2026-09-07 above) was NOT what the code did: real and
  stub verify() both collapsed a timed-out target run into "fail",
  so a pass/timeout or fail/timeout mix across reruns read as a
  consistent outcome and was never flagged flaky (worst variant: a
  test that passed once then hung read as a stable PASS). Fixed in
  execution/verify.py AND harness/_stubs/verify.py: outcome labels are
  now three-valued ("pass"/"fail"/"timeout"; timeout = timed_out OR
  exit 124), flaky = >1 distinct label. target_test_passed still
  reflects the LAST run (a timeout => not passed). No signature/schema
  change. Regression-tested with real Docker (tests/test_verify.py:
  timeout/fail mix and pass/timeout mix both flagged flaky).
- 2026-09-22 (unified plugins/connectors round): **plugins + skills +
  MCP connectors unified behind `neo plugin` / `neo skills` / `neo mcp`.
  All additive; NO boundary signature or state.json schema changes; the
  hostile-verb deny list and registry-out-of-scope decisions stand.**
  (a) **Plugin enable/disable** — new `cli/plugins.py` surface
  (`enable`/`disable`/`is_plugin_disabled`; `<name>.disabled` marker
  beside the install dir, dir stays). `list_plugins` entries gain
  `enabled` (disabled installs still LIST, marked `(disabled)`); every
  discovery consumer skips them: harness skills scan, CLI command
  roots, tool-verb extension, MCP label resolution. Reinstall clears a
  stale marker (fresh install = enabled); remove clears it too. CLI:
  `neo plugin enable|disable <name>` (PluginError → exit 2). (b) **MCP
  connectors** — new module `cli/connectors.py`: three configured
  layers (global settings `[mcp_servers]` table via
  `neo mcp add <label> -- <cmd...>` / `remove` — written through
  cli.neoconfig's existing `_read_settings`/`_dump_toml`/
  `_atomic_write_text` machinery, never overwriting a broken file;
  project `<repo>/.neo/connectors.toml` committable no-secrets; local
  `<repo>/.neo/connectors.local.toml` personal overrides) merged with
  enabled plugins' `mcp_servers` at precedence plugin < global <
  project < local by `discover_mcp_servers` (the single discovery every
  consumer uses). `neo mcp list` (merged view, source + masked
  command), `neo mcp health` (spawn + list-tools per server, ok/fail
  lines, exit 1 on any fail, never a traceback), secrets masked for
  display. `neo mcp list-tools/call` and the agent loop's `mcp` tool
  resolve configured labels first (raw commands still pass through).
  (c) **`neo skills list/show`** — read-only view over
  `harness.skills.discover_skills` (name + origin + description head;
  body on demand); plugin skills keep origin "plugin" in the planner's
  `## Applicable skills` section via the EXISTING scan (no prompt
  edits). NIGHT-B owns the `/mcp` + `/skills` slash names — this round
  defines no slash commands and touches no slash table, only the
  `cmd_mcp`/`cmd_skills`/`list_skills_for_cli`/`show_skill_for_cli`/
  connectors callables it consumes. T1 (harness): `agent_loop`
  `_resolve_mcp_server`/`_mcp_server_labels` now consult connectors
  first and skip disabled plugins (config `agent_mcp_servers` still
  wins). New tests: tests/test_cli_connectors.py (19: global
  persistence, label validation, project/local precedence, plugin
  discovery + disable, health ok/fail, masking, label resolution) +
  tests/test_cli_plugins.py gains enable/disable + skills-list/show
  pins. Full CLI sweep green except Docker-gated e2e (daemon down —
  skills/JSON e2e fail at baseline verify before any touched code
  runs, same as the pre-existing suites).
- 2026-09-25 (Terminal 11 security/privacy/observability round): **additive
  shared trust-boundary primitives**. `shared.security` owns recursive secret
  detection/redaction, fail-closed path/symlink containment, environment
  scrubbing, source-labelled adversary review, supply-chain manifest checks,
  and approval audit records. `shared.privacy` adds local-only/redacted/
  shareable derived views; `shared.retention` adds dry-run age/byte/keep
  policies and deletion receipts; `shared.telemetry` adds cost/token/context/
  event/provider-health metrics; `shared.otel` adds read-only OTLP/JSON export.
  Existing `shared.tracing.emit` now redacts fields, refuses unsafe trace
  destinations, and feeds opt-in telemetry; `shared.traceview` resolves
  sources through containment and exposes `--privacy`. No existing boundary
  signature or authoritative trace schema changed. Cross-module producers and
  consumers must opt into the shared APIs; security failures raise typed
  errors rather than warnings. Handoff: `logs/architecture-round/terminal-11.json`.
- 2026-09-25 (Terminal 03 context engine): **additive harness context/LSP
  surfaces**. `harness.context_compiler.ContextCompiler` produces a cited,
  role/provider-budgeted `ContextBundle` with hierarchical project
  instructions, acceptance criteria, weighted repository maps, exact ranges,
  relevant turns, decision memory, skills, optional diagnostics, and dependency
  context; `ContextCache` keys include repository source/index digests and
  `ContextBundle.compact` retains lossless source references. `harness.retrieval`
  adds `rank_symbols`, `rank_repository_map`, `retrieve_exact_symbol`,
  `retrieve_range`, `changed_symbol_context`, and opt-in citation receipts
  while preserving the historical four-key `retrieve_context` result.
  `harness.lsp` exposes `LspManager`, `Diagnostic`, and `get_diagnostics` as
  an optional stdlib JSON-RPC boundary; absent, failed, and slow servers return
  explicit unavailable/timeout receipts rather than raising. The compiler is
  not yet wired into the historical core/agent model requests; that integration
  is an exact Terminal 01 handoff in
  `logs/architecture-round/terminal-03.json`. `LspManager` document methods
  accept only normalized paths under `cwd`/`root_uri`; outside paths and
  symlink components return `False`, `None`, or `[]` without issuing a
  request. A valid pull response with an empty `items`/`diagnostics` list
  clears cached diagnostics for that document; an unavailable request falls
  back to the last notification cache. `Diagnostic.from_payload` and
  `get_diagnostics` are the stable normalization boundary.

### Public LSP callable contract (Terminal 03 → T4/evaluation)

The following signatures are the integration contract; callers must not reach
into `_ProcessTransport` or rename the AST lint path as LSP:

```python
LspManager(
    command: str | Sequence[str] | None = None,
    cwd: str | Path | None = None,
    timeout_s: float = 5.0,
    enabled: bool = True,
    transport: Any = None,
    root_uri: str | None = None,
    env: Mapping[str, str] | None = None,
)
LspManager.start() -> bool
LspManager.open_document(
    path: str | Path,
    text: str | None = None,
    language_id: str | None = None,
    version: int = 1,
) -> bool
LspManager.update_document(
    path: str | Path,
    text: str | None = None,
    version: int | None = None,
) -> bool
LspManager.get_diagnostics(
    path: str | Path | None = None,
    timeout_s: float | None = None,
) -> list[Diagnostic]
LspManager.shutdown() -> bool
Diagnostic.from_payload(payload: Mapping[str, Any], file_hint: str = "") -> Diagnostic
Diagnostic.as_dict() -> dict[str, Any]
get_diagnostics(
    source: Any = None,
    path: str | Path | None = None,
    timeout_s: float | None = None,
) -> list[Diagnostic]
```

`start()` sends `initialize` then `initialized` and returns `False` with
`last_error` when disabled, unavailable, or failed. Document paths must resolve
under `cwd`/`root_uri`; traversal and symlink components are refused.
`open_document` must precede `update_document`; updates send a full
`textDocument/didChange`. `get_diagnostics` prefers pull
`textDocument/diagnostic`, returns normalized `Diagnostic` objects, uses
notification cache only when the request is unavailable, and treats an
explicit empty `items`/`diagnostics` response as authoritative. `shutdown()`
is safe to call and closes the transport. `Diagnostic` ranges retain LSP's
zero-based line/column convention. The existing AST lint contract is
`harness.lint` / `lint_gate` / `lint_failed`; it is not an LSP substitute.

## Ceiling Terminal 05 — knowledge binding (harness/knowledge.py)

`KnowledgeContext` is the one object that binds the four knowledge
capabilities a run needs. It owns no new implementation: it owns one
`ContextCompiler`, one `LspManager`, and one `DecisionStore`, and degrades
each independently.

```python
KnowledgeContext(
    repo_path, config=None, *, run_id="", session_id="", task_id="",
    model="", provider="", issue_text="", target_test="",
    compiler=None, lsp_manager=None, event_hook=None,
)
KnowledgeContext.compile(issue_text="", target_test="", force=False) -> dict
KnowledgeContext.context_block(title="Compiled repository context") -> str
KnowledgeContext.citation_index() -> list[str]
KnowledgeContext.read_symbol(symbol, path="", *, max_lines=400) -> str
KnowledgeContext.find_definition(symbol, path="") -> str
KnowledgeContext.find_references(symbol, path="", *, max_results=60) -> str
KnowledgeContext.blast_radius(symbol="", *, paths=(), depth=1, max_files=25) -> str
KnowledgeContext.start_lsp() -> bool
KnowledgeContext.sync_document(relative_path, text=None) -> bool
KnowledgeContext.note_edit(relative_path, text=None) -> list[dict]
KnowledgeContext.collect_diagnostics(relative_path="") -> list[dict]
KnowledgeContext.diagnostics_note(relative_path="", *, clear=False) -> str
KnowledgeContext.record_memory(
    text, *, category="convention", verified=None, source="agent",
) -> dict
KnowledgeContext.find_recorded(text, *, category="") -> int | None
KnowledgeContext.recalled(limit=6) -> list[dict]
KnowledgeContext.lsp_status() -> dict
KnowledgeContext.close() -> dict
KnowledgeContext.as_dict() -> dict
memory_provenance(*, repo_path="", session_id="", run_id="", task_id="",
                  source="agent", model="", provider="", verified=None) -> dict
render_diagnostics_note(diagnostics, *, title=..., max_chars=2000) -> str
render_symbol_result(records, *, header, empty, max_chars=6000) -> str
```

Guarantees a caller may rely on:

- **Compile once per run.** `compile()` returns the same receipt on every
  later call (`memo_hit: True`) and the same `context_block()` string, so the
  injected context never churns between turns. `force=True` recompiles.
- **Never raises.** A missing repository, a broken compiler, an absent
  language server, a closed store, and an unreadable index all return a
  structured value and a `warnings` entry. Teardown failures are reported in
  the `close()` receipt.
- **Absence is stated honestly.** When the index cannot be read, the symbol
  tools say the index is unavailable and explicitly say that this is *not* a
  claim that the symbol does not exist. `find_references` and
  `blast_radius_for` report `resolution: "name_based"`; the result is a list
  of files to CHECK, not proof of breakage.
- **Memory writes are gated, not filtered.** Every write passes
  `shared.security.authorize_memory_write` first, so a row claiming system
  authority is quarantined and a credential-shaped row is refused. Text
  asserting an unverified outcome is stored as `category="observation"`.
  A write is refused with a reason; it is never silently dropped.
- **Dedupe is repository- and category-scoped** over normalized text, and a
  duplicate reports the first record's id, which is how a later session
  demonstrates reuse instead of duplication.

`harness/retrieval.py` additions (all take an optional `index_root` and
report `available: False` rather than a fabricated empty answer when the
index cannot be built):

```python
find_definitions(repo_path, symbol, target_file=None, *,
                 include_source=True, index_root=None) -> dict
find_references(repo_path, symbol, target_file=None, *,
                include_source=True, index_root=None, limit=60) -> dict
read_symbol_records(repo_path, symbol, target_file=None, *,
                    index_root=None, limit=400) -> list[dict]
read_symbol_by_search(repo_path, symbol, *, index_root=None,
                      per_file=2) -> list[dict]
blast_radius_for(repo_path, changed_symbols=None, changed_files=None, *,
                 depth=1, limit=25, index_root=None) -> dict
retrieval_ablation(repo_path, queries, expected=None, *,
                   index_root=None, limit=10) -> dict
```

`retrieval_ablation` scores the same queries with the PRODUCTION lexical
scorer (`production_arm="lexical_structural"`) and with a hybrid scorer that
adds a bounded character-ngram term (`ablation_arm="hybrid_ngram"`), and
reports `lexical_recall_at_k`, `hybrid_recall_at_k`,
`hybrid_minus_lexical`, and per-query agreement. It calls no model and opens
no network connection, so it is a measurement probe rather than an embedding
service. Recall is only computed for LABELED queries; an unlabeled query
reports its ranking and increments `unlabeled_queries`, and recall stays
`None` rather than becoming a fabricated `0.0`.

- 2026-09-26 (Ceiling Round 2 / R2-06 - editing correctness): **NEW
  `harness.editor.apply_text_edit` + `harness.editor.EditSession`; the edit
  refusal vocabulary is now ONE set shared with the kernel. `harness/config.py`
  gained `require_edit_digest` (None), `edit_post_check` (True),
  `edit_ambiguity_candidates` (5) and `edit_candidate_context_bytes` (120).
  `harness/tools.py` gained the `EDIT_ERROR_*` re-exports,
  `edit_refusal_vocabulary()` and `apply_catalog_edit()`. No Boundary 0-5
  signature, tool-catalog entry, event kind, serialized field, completion
  status or verifier condition changed; the catalog is still the same ONE list
  and no existing entry was modified.**
  (1) `harness/editor.py` - `apply_text_edit(root, relative_path, old_string,
  new_string, *, session=None, config=None, validate=None, trace=None) ->
  EditOutcome` is the one exact-block edit primitive. It REFUSES an ambiguous
  target (`ambiguous_match`, carrying the candidate line numbers and context)
  instead of taking the first match, REFUSES a missing target as `no_match`,
  splices the replacement on the RAW BYTES (so untouched content keeps its
  encoding, BOM and line endings exactly, and a `newlines="mixed"` file stays
  mixed where the edit did not touch), REFUSES a file whose encoding cannot be
  determined (`undetermined_encoding`) rather than reading it with
  `errors="replace"`, writes and rolls back through the repository's EXISTING
  atomic-write primitive (`execution.workspace._atomic_write_bytes`), and on a
  failed post-edit gate returns `post_check_failed` with `rolled_back=True` and
  the file byte-for-byte restored. `EditOutcome` carries the full receipt
  (`encoding`, `newline`, `match_count`, `candidates`, `rolled_back`,
  `digest_required`, `digest_relaxed`, `digest_relaxed_reason`, `write_mode`,
  `pre_sha256`, `post_sha256`) and is JSON-safe. Every refusal is a value, never
  an exception, and is written to `trace` as `edit_applied` /
  `edit_rolled_back` / `edit_refused`.
  (2) `EditSession` is the read ledger `require_edit_digest` is enforced
  against - one instance per run, owned by the caller, with no module-level
  state. `note_read(rel, data=None, *, root=None)` records a real observation,
  `note_mutation` re-baselines after an applied edit, `was_read`/`revision`
  read it back. It is the same mechanism as the kernel's digest binding, not a
  second convention.
  (3) `harness/editor.py::syntax_check` gained ONE additive keyword-only
  parameter `encoding=None`. With it (the new primitive passes the encoding it
  detected) a UTF-8-BOM or latin-1 Python file is checkable at all; without it
  the historical `utf-8`/`errors="replace"` read is byte-for-byte unchanged, so
  every existing call site is identical.
  (4) `harness/tools.py` (edit path only) re-exports the editor's slugs as
  `EDIT_ERROR_*`, adds `EDIT_ERROR_VALIDATION`, exposes
  `edit_refusal_vocabulary()`, and adds `apply_catalog_edit(root, arguments,
  *, session, config, trace)` which schema-validates a catalog `edit` call and
  then applies it through the primitive, BINDING `expected_revision` from the
  session exactly as `strategy._bind_harness_arguments` does.
  `harness/agent_kernel/tools.py` is UNCHANGED; the three shared slugs are
  pinned equal to its literals by
  `tests/test_ceiling_r2_06_editing.py::test_the_edit_refusal_slugs_are_the_kernels_slugs`.
  (5) `require_edit_digest` is `None` in `DEFAULTS` and the strict reading is
  the PRIMITIVE's floor: an absent key, a `None` value, or `True` all mean "on"
  for `apply_text_edit`, and only an explicit `False` relaxes it - with
  `digest_relaxed=True` and a reason on the receipt. The GLOBAL default was
  measured before it was set: `True` in `DEFAULTS` takes the daily-driver matrix
  from 52/52 arms ok to 41/52 and flips
  `readiness.zero_false_verified_successes` from True to False, because
  `strategy._bind_harness_arguments` is not unconditional (it returns early
  with no `execution_backend`, and its table covers only edit/rename/delete) so
  a strict default refuses real mutations and the run still finishes. A default
  that produces a false verified success is worse than a missing default, so
  the value is None and the blocker is pinned by
  `test_the_harness_default_leaves_the_typed_kernel_path_lenient_on_purpose`.
  The change needed to lift the blocker is filed in `harness/AGENTS.md` under
  "Cross-terminal requests".
  Verified: `python -m pytest tests/test_ceiling_r2_06_editing.py -q -p no:randomly`
  -> **34 passed** (all seven required proofs, including a REAL crash at
  `os.replace` via `tests/editor_crash_driver.py`, a CRLF round trip, a latin-1
  round trip, and a byte-for-byte rollback).
  `tests/test_ceiling_r2_06_editing.py tests/test_editor_prompts.py
  tests/test_tool_protocol.py tests/test_workspace_security.py
  tests/test_agent_kernel.py tests/test_agent_loop.py
  tests/test_config_trace_state.py tests/test_adversarial.py` -> **305 passed,
  1 skipped**. `python -m evals.run --check` -> **14/14 CLEAN**;
  `python -m evals.run --quick` -> **40/40, verdict CLEAN, 0 regressions**;
  `python -m evals.run --suite daily-driver --no-docker --json` -> **52/52
  arms ok, `zero_false_verified_successes=true`**, overall readiness
  `NOT_READY` because the Docker and live-provider lanes were not selected.
  **No Docker lane and no live-provider lane were run and neither is claimed.**

- 2026-09-27 (Ceiling Round 2 / R2-12 - polyglot via an ecosystem registry):
  **NEW `execution/ecosystems.py`; `execution/verify.py` consults it; ONE additive
  keyword-only parameter on `harness.editor.extended_protected_patterns`; two
  `None`-valued `DEFAULTS` entries. No Boundary 0-5 signature, event kind,
  serialized field or completion status was changed, removed or renamed.
  `shared/types.py` was NOT edited and `harness/_stubs/verify.py` is untouched,
  so the new attributes are ADDITIVE INSTANCE ATTRIBUTES on
  `VerificationResult` and the existing call shape stays valid.**

  (1) **THE registry, consulted everywhere.** `execution.ecosystems` is the one
  per-language table: suite/build/lint/format commands, test-file globs,
  protected-path globs, the structured-result format, the zero-test policy and
  its markers, the toolchain binaries, the sandbox base image, the dependency
  manifests, the target-command template, and the protected
  test-configuration surfaces. Public surface: `SCHEMA_VERSION`;
  `FORMAT_{PROSE,JUNIT_XML,PYTEST_JSON,GO_TEST_JSON}`; `CAPTURE_{NONE,
  SENTINEL,INLINE}`; `ZERO_TEST_{PARSER,FAIL_CLOSED}`; `COMMAND_{PYTEST,JEST,
  TEMPLATE}`; `OUTCOME_{PASS,FAIL,TIMEOUT,ERROR,NO_TESTS_COLLECTED,
  TOOLCHAIN_UNAVAILABLE}`; `GATE_OUTCOMES`; `TOOLCHAIN_UNAVAILABLE_MARKER`;
  `MAX_REPORT_BYTES`; `MAX_RECEIPT_CASES`; `DEFAULT_REPORT_PATH`;
  `EcosystemPolicyError`; `TestOutcome`; `Ecosystem`;
  `register_ecosystem(eco, *, replace=False)`, `ecosystem(name)`,
  `ecosystems()`, `ecosystem_names()`, `detect_ecosystem(repo_path)`;
  `shell_quote`, `regex_escape`, `split_target`, `suite_command`,
  `target_command`, `toolchain_probe_command`, `compose_run_command`,
  `split_report`, `new_report_token`; `parse_junit_cases`,
  `parse_go_test_cases`, `parse_go_test_json`, `normalize_structured`,
  `parse_cases`; `toolchain_unavailable_reason`, `zero_test_reason`,
  `gate_outcome`, `blocks_success`, `render_receipt`;
  `effective_protected_paths`, `matches_any_glob`, `is_test_path`; the seven
  built-ins `GO`, `RUST`, `JAVA`, `DOTNET`, `RUBY`, `JAVASCRIPT`, `PYTHON`.
  **ONE language detector, never two**: the `pytest` and `jest` families
  delegate command composition to `execution.verify`'s EXISTING
  `_autodetect_test_command` / `_target_command`, so a Python or JS repository
  is byte-identical to before; a `runner_template` ecosystem composes from
  registry data. `register_ecosystem` FORWARDS `Ecosystem.surfaces` into
  `harness.test_config.register_language_surfaces` - the seam that module's own
  comment reserved for this round - inside a guarded import.

  (2) **Zero collected tests is its own loud outcome, and the policy cannot be
  weakened.** The parser-level `no_tests` is promoted to the distinct
  gate-level `no_tests_collected` in the receipt, in an `ecosystem`-sourced
  `reports` row, and in a `## ecosystem` block appended to `raw_output`. It is
  never `pass`, never `flaky`, never `skipped`, and `blocks_success()` is True.
  `ZERO_TEST_POLICIES` is a CLOSED set of two, BOTH fail-closed, and
  `Ecosystem.validate()` raises `EcosystemPolicyError` for anything else - so a
  future language cannot be registered into the vacuous-green class.
  `fail_closed` additionally requires POSITIVE collected-test evidence before a
  zero exit may pass, which is what catches Go's `? pkg [no test files]` -> exit
  0. Measured on this round's own fixture: `parse_test_run` reports `outcome=
  "pass", tests_collected=None` for that capture, and `gate_outcome` reports
  `no_tests_collected`. The refusal is doing work, not restating the parser.

  (3) **The JUnit XML channel is FED, and the verifier consumes structured
  results instead of scraping text.** `parse_test_run(junit_xml=...,
  json_report=...)` already existed and had NO production caller anywhere in the
  tree. For a `sentinel` ecosystem the composed command appends the runner's
  report argument, preserves the runner's own exit code across the report echo
  (`{ cmd; } ; __neo_rc=$? ; ... ; exit $__neo_rc`), and echoes the report
  inside a PER-CALL sentinel block keyed by an unguessable token, so test OUTPUT
  cannot forge a report. `split_report()` restores `raw_output` to exactly the
  runner's own bytes, which is what keeps the `execution.feedback` and
  `execution.rationale` transcript parsers unaffected. The report is written
  INSIDE the container (`/tmp/neo-structured-result.xml`), never into the
  bind-mounted repository, so it cannot become part of the delivered diff.
  Verified against a REAL pytest `--junitxml` run in a REAL container: both
  `reports` rows read `source="report", confidence="high"` with the runner's own
  collected/skipped counts, and the per-test `<testcase>` elements reach the
  receipt as `TestOutcome`s. Go uses `CAPTURE_INLINE` - `go test -json` is Go's
  own event stream - normalized into the same channel shape.

  (4) **Per-test outcomes reach the receipt.** `parse_junit_cases` and
  `parse_go_test_cases` return the runner's OWN rows; they ride on
  `result.test_outcomes` (JSON-safe, bounded at `MAX_RECEIPT_CASES` in the
  block, which always prints the true total) and the FAILING ones are MERGED
  into the existing `structured_feedback` after the prose parser has run, so the
  model-facing channel is sharpened rather than replaced.

  (5) **A missing toolchain is a refusal, never a pass.** Each ecosystem declares
  its binaries and the composed command is prefixed with a `command -v` check
  that prints the stable `TOOLCHAIN_UNAVAILABLE_MARKER` and exits 127 (the
  shell's own command-not-found code). A registry language with no installed
  toolchain therefore reports `toolchain_unavailable`, which is deliberately
  NOT conflated with `no_tests_collected` - one is a broken machine and the
  other is a suite that ran nothing. The probe costs no extra container and
  cannot change a present toolchain's exit code.

  (6) **Per-language protected paths.** `DEFAULTS["protected_paths"]` is
  Python-shaped (`["tests/*", "test_*.py", "*_test.py"]`) and matches NO Java
  or Go test file, so a Maven or Go repository had no protected test surface at
  all. `effective_protected_paths(configured, eco)` KEEPS the operator's globs
  (they are policy, not a default) and ADDS the ecosystem's own, and
  `harness.editor.extended_protected_patterns` gained ONE additive keyword-only
  `ecosystem=` parameter (an `Ecosystem` or a registered name) to reach it.
  `is_protected`'s two-positional-argument signature is UNCHANGED.

  (7) **Config discipline.** Two `None`-valued `DEFAULTS` entries
  (`ecosystem`, `ecosystem_protected_paths`), which is behaviour-neutral because
  `None` is what an ABSENT key already means to both consumers. `DEFAULTS`
  itself is otherwise untouched, and
  `test_protected_paths_default_is_untouched_by_this_round` pins the Python
  default. `test_no_ecosystem_default_switches_every_run` fails if either key
  is ever given a real value.

  (8) **Verified.** `tests/test_ceiling_r2_12_polyglot.py` -> **79 passed, 1
  skipped** (real Docker daemon 28.5.1). The five required proofs are present
  and named; the skip is the Go-TOOLCHAIN lane, which is IMAGE-gated and
  self-skips with its reason, i.e. BLOCKED coverage and not a pass. Of the
  other four, two are real-Docker proofs: a real pytest JUnit report consumed
  with its per-test outcomes, and a real Go repository in the real sandbox
  degrading to `toolchain_unavailable`. Regression selections:
  `test_verify.py test_verify_js.py test_feedback.py` -> **93 passed**;
  `test_ceiling08_verification.py` -> **78 passed**;
  `test_verification_gate_wiring.py test_ceiling_r2_02_flake.py
  test_ceiling_r2_05_baseline.py test_ceiling_r2_03_config_guard.py` ->
  **176 passed, 1 failed**; `test_editor_prompts.py test_config_trace_state.py
  test_stubs_and_deps.py test_adversarial.py test_ceiling_r2_06_editing.py
  test_ceiling_r2_07_codemod.py` -> **207 passed, 1 skipped**;
  `test_sandbox.py` -> **61 passed**; `test_e2e_run_task.py
  test_git_output_rationale.py` -> **59 passed**; `test_modes.py
  test_coordination_e2e.py` -> **104 passed**. `python -m evals.run --check` ->
  **14/14 CLEAN**. `python -m evals.run --suite daily-driver --no-docker
  --json` -> **50/52 case arms ok, `zero_false_verified_successes=true`**,
  readiness `NOT_READY` (the Docker and live-provider lanes were not selected).
  `ruff check` clean on every owned file; `compileall` clean; both NEW files
  are `ruff format` clean. **No live-provider lane was run** and no credential
  was inspected or retained.

  (9) **Two red results, both PROVEN not from this round, neither weakened.**
  (a) `test_ceiling_r2_02_flake.py::test_the_added_wall_time_of_repetitions_is_
  measured_and_linear` is a host wall-clock pin (`elapsed(3 reps) <= 3*elapsed(1
  rep) + 1.0`); it measured 7.48s against a 7.09s bound on a loaded host and
  passes 3/3 standalone. It exercises `execution.flake_gate` over
  `execution.flake.run_local_command`, the HOST lane, and the only references
  to `execution.verify` in either module are DOCSTRINGS.
  (b) `dd_20_live_tui_status_diff` fails both arms on
  `live_diff_populated` - a Textual transcript poll for the string `+value = 2`.
  **Controlled A/B, not recollection:** with
  `execution.verify.detect_ecosystem` forced to return `None` via a
  `sitecustomize` hook on `PYTHONPATH` (so the byte-identical pre-R2-12 path is
  taken in the driver AND every worker subprocess), the arm is STILL `failed`
  with `live_diff_populated=false`. R2-06's own AGENTS.md already records this
  arm as timing-flaky under load.
  (c) Three assertions in `tests/test_ceiling08_verification.py::TestIncremental
  Selection` pinned the literal DISPATCHED command, which legitimately changed.
  They were made STRONGER, not weaker: they now assert the claim directly (the
  full suite ran AND no test path leaked into the gating run) via a documented
  `_runs_the_full_suite_only` helper, instead of by exact string equality. This
  is a disclosed edit to one file outside this round's declared set, and no
  assertion was relaxed.

  (10) **NOT implemented / BLOCKED.** `execution/sandbox.py` has no Go base
  image and no hook for one, so a real `go test` cannot run in the sandbox and
  the one proof that needs the Go toolchain is IMAGE-gated. That file is
  another terminal's in-flight edit and was not touched; the request is written
  out in `execution/AGENTS.md`. Per-language protected paths are reachable
  through `effective_protected_paths` and `extended_protected_patterns`, but the
  two resolution points in `harness/core.py` (`cfg.get("protected_paths")` at
  :669 and :2540, and the `_authorized_test_target` strip beside them) still
  read the Python-shaped list directly, so the wiring is a filed request too.
  `VerificationResult` has no real fields for the receipt (another owner's
  file), so `ecosystem`, `ecosystem_gate`, `no_tests_collected`,
  `toolchain_unavailable`, `zero_test_policy` and `test_outcomes` are additive
  instance attributes and are NOT round-tripped by `runtime/serialize.py`.
  Rust/Java/.NET/Ruby are registered as DATA and are exercised by
  `test_a_brand_new_language_gets_a_full_contract_from_data_alone`-style proofs
  plus the composition tests, but no test lane runs their real toolchains.
  JavaScript has no structured channel: jest's `--json` is deprecated and
  vitest's junit reporter needs a dependency, so its format is honestly `prose`.

- 2026-09-28 (AGT-10 - queued messages at the tool-batch boundary): **NEW
  public surface in three files; no Boundary 0-5 signature, event kind
  vocabulary, serialized field, or completion status changed, and the mint
  condition is untouched.** `harness/steering.py` gains
  `QueuedSteering` / `QueuedSteeringWatcher` / `SteeringDelivery` /
  `partition_intents` plus ONE method on `SteeringBuffer`,
  `append_record(obj)`; `harness/tools.py` gains `run_tool_batch` /
  `ToolBatch` / `ToolBatchStep`; `harness/agent_loop.py` (the
  `_run_agent_legacy` turn loop) routes its turn's tool batch through
  `run_tool_batch` and consumes at a batch-boundary seam.
  `harness/core.py`, `harness/agent_kernel/**`, `harness/config.py` and every
  verifier were NOT edited.** (1) **The transport is unchanged.**
  `logs/{task_id}/steering.jsonl` is still the only channel and
  `SteeringBuffer.take` is still the only writer of a `consume` row. Two new
  `op` values are added to the same file, `queue` and `deliver`, appended
  through the same single-write append; `_scan` folds ONLY `inject` and
  `consume`, so the new rows are audit, not state, and a replayed pending set
  can never depend on one. (2) **`run_tool_batch(calls, dispatch, seam=...)`
  is the seam primitive**: `dispatch` is invoked exactly once per call and
  never re-entered, split or interrupted, and `seam` is called only BETWEEN
  calls (after the last one too), so a mutating call can never be split to
  make room for a queued message. A `seam` that raises or returns False stops
  the batch and the untouched calls are named in `not_executed`. (3)
  `QueuedSteering.deliver(where, turn=, tool=)` is a thin wrapper over
  `SteeringBuffer.take`; `SteeringDelivery.action()` is the strongest intent
  (`abort > replan > guide`) and `partition_intents` is the single definition
  of that precedence, shared by the turn boundary and the seam so two
  consumers cannot disagree. (4) **`QueuedSteering.receipt()` is the audit**
  and `undelivered` is the load-bearing key: the seqs the queue saw and never
  handed to a consumer. It is reported on a `steering_queue` trace row and on
  `AgentResult["steering_queue"]`. Every consume in the legacy loop goes
  through the queue, including the pre-dispatch and in-flight aborts, so the
  receipt cannot under-report a real delivery. (5) **The final gate is a
  STRENGTHENING**: `DONE` is refused while the queue is non-empty
  (`steering_final_gate_deferred`) and the run continues. This round added a
  consume point, so a typed instruction could otherwise be swallowed by the
  turn that ends the run; with `steering_enabled: False` the queue is empty
  and the branch cannot fire. No completion status, no `TaskResult` field and
  no verifier behaviour changed. (6) **Four additive trace kinds**:
  `steering_batch_boundary`, `steering_queue`,
  `steering_queue_watcher_error`, `steering_final_gate_deferred`; the
  loop-side receipts keep the historical `steering` / `steering_replan` /
  `steering_abort` kinds, the historical `where` slugs, and their historical
  payload keys (with `waited_s` added to `steering`). (7) **No `DEFAULTS`
  key** was added; see `harness/AGENTS.md` AGT-10 section 6 for why a default
  there would silently switch every task and every eval arm. Verified:
  `tests/test_agt_10_batch_boundary.py` -> **23 passed** (host-only);
  `test_agent_loop.py` -> 62 passed; `test_agt_02_reflection.py` +
  `test_recovery_steering.py` -> 89 passed; a 5-file neighbour selection -> 127
  passed, 1 skipped; `test_streaming_ui.py` + `test_modes.py` +
  `test_cli_runview.py` -> 279 passed; `python -m evals.run --check` -> 14/14
  CLEAN; `ruff check` and `compileall` clean. Daily-driver: 49/52 arms,
  `zero_false_verified_successes`/`zero_unauthorized_mutations`/`zero_lost_edits`
  all true; the 3 red arms are attributed in `harness/AGENTS.md` section 9 and
  none of them is on this round's path. **No Docker lane and no live-provider
  lane were run.** Not implemented: the typed kernel's strategy has no steering
  integration at all and no seam to attach one to; `harness/core.py`'s fix
  loop is unchanged; no CLI surface was added.

- 2026-09-28 (AGT-09 - staged undo, three restore granularities): **additive
  contracts only; no Boundary 0-5 signature, event kind, serialized field,
  exit code, completion status, or verifier mint changed, and no
  `harness/config.py` `DEFAULTS` key was added.** `memory/checkpoints.py` gains
  `StagedSnapshotStore` plus the module surface
  `capture_staged_snapshot` / `stage_staged_revert` / `widen_staged_revert` /
  `staged_revert_state` / `staged_revert_plan` / `commit_staged_revert` /
  `discard_staged_revert`, and the constants `RESTORE_SCOPES`
  (`files` | `conversation` | `both`), `DEFAULT_RESTORE_SCOPE` (`files`), and
  `EXCLUSION_REASONS`. `harness/editor.py` gains `StageCapture`,
  `stage_capture_scope`, `active_stage_capture`, and `note_changed_paths`, all
  additive and keyword-defaulted. `cli/fileview.py` gains the read-only
  projection and the ONE shared dispatcher `undo_command` (plus
  `staged_undo_state`, `stage_undo`, `undo_plan`, `commit_undo`,
  `discard_undo`, `commit_staged_undo_for_prompt`, `undo_live_refusal`,
  `undo_scopes`, `undo_scope_for`, `undo_scope_words`, `undo_turn_rows`,
  `render_undo_receipt`, `UNDO_VERBS`, `UNDO_SCOPE_ALIASES`).
  (1) **A SECOND mechanism, not a second call.** A checkpoint stack is not
  scriptable, not idempotent, and cannot express a RANGE. The staged store is
  a content-addressed private object store (`<log_root>/_undo/<session>/objects`)
  plus an append-only journal; a snapshot id is a digest of its CONTENT, so
  re-capturing an unchanged state writes nothing and journals nothing. (2) **No
  commits, no branch movement, no index change.** The only bytes written inside
  the repository are the file content a committed revert is asked to put back.
  (3) **`RESTORE_SCOPES` is the product's EXISTING rewind vocabulary**
  (`harness.agent_kernel.context.rewind_run`'s `files` / `conversation` /
  `both`), pinned equal by a test because `memory` may not import `harness`.
  `files` is the DEFAULT because rewinding code and KEEPING the conversation is
  what a user wants almost every time. (4) **Staging widens, it never pops**:
  `/undo` stages the newest turn, a second `/undo` WIDENS the range, and a new
  prompt COMMITS it. Only a `files` range auto-commits on a prompt; a
  `conversation` or `both` range rewrites history and requires an explicit
  `/undo commit`. (5) **The revert is hash-verified in both directions** and
  the receipt names `restored`, `unchanged`, `refused`, `excluded`,
  `overwritten_user_edits`, `conversation`, `forced`, and `verified` (which is
  False when nothing was checked, because `all([])` is True). (6) **A concurrent
  user edit is refused, not overwritten**: a path whose current hash appears
  NOWHERE in the staged range is refused, while the run's own pre- and
  post-images are ACCOUNTED, so a plain revert still works. `force=True` is
  possible but records every overwritten user edit under its own key. (7) **A
  live run refuses the revert** at the memory boundary (`commit(live_run=True)`)
  and at the CLI (`undo_live_refusal`), and a refusal leaves the range STAGED so
  the user's intent is not lost. (8) **Exclusions are reported, never silently
  skipped**: `gitignored` (asked of `git check-ignore` in ONE batched call, with
  `ignore_authority` naming whether git or the built-in set answered), `too_large`,
  `out_of_scope`, `symlink`, `unreadable`, `not_a_regular_file`, and
  `checkpoint_storage`; the receipts carry them forward and the renderer prints
  them. (9) **Capture is best-effort by contract**: `capture()` NEVER raises, a
  failure is a `status="failed"` record plus an `undo_snapshot_failed` event,
  and `harness.editor`'s hooks swallow their own exceptions. (10) **A bare
  `/undo` with NO captured turn hands the line back to the historical per-run
  engine**, so a session that predates (or misses) the capture seam is still
  revertible, and `/undo <file>` still reaches the per-file revert. Verified:
  NEW `tests/test_agt_09_staged_undo.py` -> **68 passed, 1 Windows
  symlink-privilege skip** (a blocked platform case, not a pass) - every
  required proof is present and named; neighbour lanes
  `test_agent_kernel` + `test_agent_loop` + `test_config_trace_state` +
  `test_adversarial` + `test_workspace_security` + `test_tool_protocol` ->
  **249 passed**; `test_cli_tui` + `test_cli_tui_layout` + `test_tui_contract` +
  `test_cli_runview` + `test_cli_tracelog` -> **359 passed**; `test_cli` +
  `test_cli_release` + `test_cli_neo2` + `test_cli_neo3` + `test_cli_errors` +
  `test_cli_config` -> **220 passed**; `test_cli_slash2` +
  `test_cli_terminal_parity` + `test_cli_command_system` + `test_cli_polish` ->
  **202 passed**; `test_cli_session` + `test_decision_store` + `test_code_graph` +
  `test_mcp_server` + `test_mcp_client` + `test_editor_prompts` +
  `test_ceiling_r2_06_editing` -> **162 passed, 2 skipped**;
  `test_memory_mcp_release` + `test_mcp_adversarial` + `test_mcp_stdio_fileno` +
  `test_dashboard` + `test_cli_fileview` + `test_cli_power_tools` ->
  **129 passed, 2 skipped**; `python -m evals.run --check` -> **14/14 CLEAN**,
  exit 0; scoped `ruff check` clean on all owned files. **No Docker lane and
  no live-provider lane were run and neither is claimed.** Cross-owner
  requests: `harness/agent_kernel/strategy.py` and `harness/agent_loop.py` can
  call `editor.StageCapture.begin_turn` / `end_turn` for a WHOLE-TREE
  `step_before` / `clean_completion` capture per turn (the mutation path
  already captures the paths it touches, which is cheaper and is what the
  feature currently relies on); `harness/config.py` may add `None`-valued
  `undo_staged_*` discoverability entries, but publishing a REAL value would
  switch every task and every eval arm and `tests/test_agt_09_staged_undo.py::
  TestHonestyGates::test_no_staged_undo_key_is_in_the_harness_defaults` will
  fail if one appears.

- 2026-10-01 (P0 / Wave 2 - T5 platform: contracts, memory, evals, CI, and the
  G0 gate): **NO CONTRACT CHANGED. NO COMPLETION STATUS, VERIFIER MINT
  CONDITION, EXIT CODE, EVENT KIND OR SERIALIZED FIELD CHANGED. NO Boundary 0-5
  SIGNATURE WAS ADDED, REMOVED OR RENAMED. `shared/types.py`, `harness/config.py`
  and every boundary owner were NOT edited.** Nothing in this entry can change
  what a run does; it is test infrastructure, one new eval package, one new
  script, and two pytest-configuration lines. Stating that plainly is the most
  reassuring sentence available, so it is first.

  **(0) WHAT DID NOT CHANGE, explicitly.** No completion status was added or
  renamed. `shared.agent_contracts.RUN_STATUSES` is byte-identical. The
  verifier mint condition (`target_passed AND regression_passed AND NOT flaky`)
  was not touched and no eval arm, default or config key widens it. No
  serialized receipt gained, lost or renamed a field. No trace event kind was
  added. No exit code changed meaning. No `Task.config` key was added. Four
  terminals sent interface requests this wave and **none of them was a contract
  change** - they were test-infrastructure requests, all of which are recorded
  below. If you are looking for something to re-audit because this entry
  exists: there is nothing to re-audit.

  **(1) NEW `tests/known_failing_pins.py` + `tests/test_known_failing_pins.py`
  (test infrastructure, not product).** The registry of deliberately-failing
  pins, and the meta-test that enforces it. Two entry classes, because the
  distinction is the whole point: `KnownFailingPin` is RED ON PURPOSE (the
  recorded gap is open) and `InvertedPin` is GREEN ON PURPOSE (it guards a
  closed-by-refusal gap, so a red is a regression). Four mechanics, each a
  separate failure mode: a known-failing pin must be **red for its stated
  reason** (>=2 `reason_substrings` must appear in the failure text); a pin that
  **passes** is a build failure carrying `PROMOTE THIS` and the instruction to
  delete the registry entry in the same change; a pin that is red but missing a
  stated substring is a **REGRESSION**, never a known gap; and a pin that
  cannot be run (missing / not-collected / timed-out) is a build failure, never
  a skip. Plus **blanket suppression is refused mechanically**: an AST pass
  rejects any `xfail`/`skip`/`skipif` decorator on a registered pin, and a
  config pass rejects an `addopts` `--ignore`/`--deselect`/`--ignore-glob`/marker
  filter. Entries are named, individually. Public surface:
  `KNOWN_FAILING_PINS`, `INVERTED_PINS`, `KnownFailingPin`, `InvertedPin`,
  `RegistryError`, `observe`, `Observation`, `classify_known_failing`,
  `classify_inverted`, `classify`, `check_all`, `Verdict`, `render_report`,
  `suppression_offenders`, `blanket_marker_offenders`, `main`, and the
  `AS_RECORDED` / `PROMOTE` / `CHANGED_REASON` / `MISSING` / `SUPPRESSED` /
  `BLANKET_MARKER` / `NOT_COLLECTED` / `TIMED_OUT` / `PASSED` / `FAILED`
  vocabulary. CLI: `python -m tests.known_failing_pins` (exit 1 on any build
  failure). **No `xfail` marker was introduced anywhere in this repository;
  the repo still has zero.**

  **(2) POPULATED FROM MEASUREMENT, and the count is ONE.** The W2 brief listed
  five known-failing pins. Measured on this tree 2026-10-01:
  - T2's unsandboxed-bash pin EXISTS and is RED on exactly one site,
    `harness/agent_loop.py:797` (`from harness._stubs import sandbox`). It is
    the single `KNOWN_FAILING_PINS` entry. Owner **T1 / P2.1**; green when that
    reach is deleted. Verified red-for-the-stated-reason by a real subprocess
    run, not by reading the docstring.
  - **T3's structural-predictor pin is NOT red, and the brief had it inverted.**
    What exists is `runtime/invariants.check_structural_guard` (**HOLDS**; 9/9
    invariants hold) and `runtime/test_structural_predictor_guard.py` (**16
    passed**). Both assert the predictor is STILL UNSHIPPED - the opposite
    sense. It is registered as an `InvertedPin`, because registering a GREEN
    pin as a known-FAILING one would have filed it wrong, which is the exact
    confusion this mechanism exists to prevent. Owner T3 / P6.
  - **No third or fourth designed-red pin exists.** The other reds in the
    module-local suites are REAL failures and are reported as `fail` in the G0
    verdict, never here.

  **(3) NEW `evals/gates/` - `__init__.py` + `P0.py` (the G0 gate).** Additive,
  opt-in, imports nothing from any other module and is called by nothing except
  its own test and CI. The four-status vocabulary is
  `pass | fail | blocked | not_implemented`, exported as `STATUSES`; **`skip`
  is deliberately NOT a member and `_status()` raises `GateError` on it and on
  every near-synonym** (`skipped`, `xfail`, `pending`, `todo`), because a skip
  renders identically to a pass in the summary table this gate prints. A
  `blocked` rung cannot be constructed without a reason and a `not_implemented`
  rung cannot be constructed without an owning phase. Public surface:
  `STATUSES`, `Rung`, `GateError`, `blocked`, `not_implemented`, `run_probe`,
  `docker_reachable`, `count_security_blockers`, `parse_pytest_log`,
  `read_log_text`, `g0_report`, `render`, `main`, `NOT_ESTABLISHED`,
  `BLOCKING_SEVERITIES`, `TRUST_LADDER_SUITE`, `LADDER`, `GATE_GREEN`,
  `GATE_RED`, and the four status constants. CLI:
  `python -m evals.gates.P0 [--tests-dir-log PATH] [--module-local-log PATH]
  [--no-probes] [--json]`, **exit 2 when a BLOCKING rung is `fail`**. A
  `blocked` row does not by itself turn the exit code 2 - blocked is a report,
  not a verdict, and the point of the vocabulary is that it is VISIBLE rather
  than disguised. **The report carries `what_this_does_not_establish` in both
  the dict and the rendered text and it cannot be dropped from either.**

  **(4) The `trust-ladder` eval suite DOES NOT EXIST.** `python -m evals.run
  --suite` accepts exactly `auto`, `combined`, `daily-driver`,
  `prompt-regression`. The Trust Ladder is reachable only as
  `tests/test_ceiling_r2_15_trust.py`, a pytest suite the eval runner cannot
  address. The W2 brief lists it as a Wave 1 deliverable; **it is not
  registered**, and `evals/run.py` was NOT edited to add it (adding a suite
  would be a behaviour change to the eval surface, and the brief's own task is
  to report, not to invent). `P0.py` therefore reports it
  `not_implemented`, owning phase **T5 / P0**, and reads the registration list
  from argparse at run time so a suite added later needs no edit to the gate.

  **(5) `pyproject.toml` `[tool.pytest.ini_options]` - the collection surface
  was LYING about coverage.** `testpaths` was `["tests"]`, so `python -m pytest`
  collected **7,169** tests and MISSED every module-local `test_*.py` inside a
  production package: **24 modules / ~426 tests**, including the entire
  known-gap registry surface (`execution/test_unsandboxed_pins.py`,
  `harness/test_egress_call_sites.py`, `harness/test_gap_audit.py`, the four
  `runtime/test_*_pins.py`, `cli/test_render_path_pin.py`). The one suite only
  T5 runs was the one suite that could not see them. Now:
  `testpaths = ["tests", "harness", "execution", "runtime", "cli", "mcp_server",
  "memory", "shared", "evals", "scripts", "dashboard", "demo"]` (**7,595**
  collected) - `tests/` stays first, and the module roots are enumerated
  EXPLICITLY rather than as `.` so a new top-level directory is a deliberate
  decision. Added `norecursedirs` for `fixtures` and `demo-work` (fixture repos
  are TEST DATA with deliberate bugs) and `addopts` with **two `--ignore`
  entries for PRODUCTION modules that collide with pytest discovery**:
  `execution/test_selection.py` (3 production importers; T2's own finding 8.6)
  and **`harness/test_config.py` (2 production importers:
  `execution/ecosystems.py`, `harness/editor.py`) - a NEW finding this wave**.
  The latter is 1,164 lines exporting `resolve_effective_test_config`,
  `diff_effective_test_config`, `classify_test_config_path` and
  `register_language_surfaces`, plus two functions literally NAMED
  `test_config_patterns` and `test_config_guard`; collected as a suite it
  yields 1 no-op test (it RETURNS a list instead of asserting) plus 1 hard
  error (`fixture 'pristine_dir' not found`) - **zero assertions and one error,
  from a module that is really the edit-gate's configuration resolver.** Both
  were found by an AST scan for "does PRODUCTION code import this module?",
  not by eye. **These are not suppressed tests** - pytest is being asked to
  import production code as a test module, which is the wrong request. If you
  add a third, rename the module; this list is a migration aid.
  `markers` is UNCHANGED (still only `slow`).

  **(6) NEW `scripts/provenance_report.py` + `tests/test_provenance_report.py`
  + a `provenance` job in `.github/workflows/release-gate.yml`.** The Phase 7
  provenance spine, seeded EMPTY on purpose: this tree vendors no third-party
  code today (measured: 742 source files scanned, 0 declared, 0 undeclared), so
  the gate is green now and turns red the first time somebody copies a file in
  without declaring it. A mechanism introduced alongside the first port is a
  mechanism that has never been wrong yet, and this repository has already
  shipped four mechanisms with no call site. The vendored-file header format is
  specified in the module docstring: `vendored`, `upstream` (a URL),
  **`upstream-commit` (a 7-40 char hex SHA - a branch or tag is not a pin,
  because it cannot be re-derived or diffed against its source)**,
  `upstream-path`, `licence`, **`beats-ours-because` (mandatory - the field
  that matters and the one everyone skips; "mature" is rejected as too short to
  be a reason)**, and `modified` (`yes`/`no`, required even when `no`, because
  an unstated modification makes a licence determination impossible). A file is
  treated as vendored by SHAPE, via three detectors: an explicit `vendored:`
  block, a vendor-shaped directory, or a third-party licence/copyright header.
  CLI: `python scripts/provenance_report.py [--json] [--strict]`, **exit 2 on
  any undeclared vendored file**; `--strict` also fails on a present-but-
  incomplete declaration. Stdlib-only on purpose, so a bad lockfile cannot
  disable it. **It cannot detect code borrowed in SPIRIT and says so in its own
  output**: this repository uses "derived" in the sense of *computed from* in
  135 files and a keyword scanner cannot separate that from *ported from*, so
  prose attribution stays a reviewer's job.

  **(7) Verification actually run (this tree, `-p no:randomly`, real Docker
  28.5.1, Windows host, Python 3.10.11).** `pytest tests/ -q` -> **15 failed,
  7,121 passed, 33 skipped in 5250.04s**. Module-local lane
  (`harness execution runtime cli mcp_server memory shared evals scripts
  dashboard demo`) -> **2 failed, 406 passed** (the designed-red pin + one real
  docs drift). `python -m evals.run --check` -> **14/14 ok, verdict CLEAN**.
  `python -m evals.gates.P0 ...` -> **`G0_RED`, exit 2** (see
  `docs/release-verdict.md`). `python -m tests.known_failing_pins` -> **exit 0,
  `as_recorded=5`, 0 build failures**. `python scripts/provenance_report.py
  --strict` -> **exit 0, PROVENANCE_COMPLETE**. New suites: 18 + 31 + 22 = **71
  passed**. `ruff check` clean on every new file.
  **NO LIVE-PROVIDER LANE RAN AND NONE IS CLAIMED** - no live provider is
  reachable (`ServiceUnavailableError: No available channel`, 3/3), so **every
  model call in this wave's evidence is a scripted double and no result here is
  a claim about model quality.** The Windows lane is DEFINED
  (`.github/workflows/windows-dockerfree-ci.yml`) but **has not been observed
  RUNNING in this session** - it was not pushed, because SG-04 records that the
  shared tree is dirty and committing requires explicit human approval.

  **(8) OPEN, OWNED ELSEWHERE - not applied here.** (a) **T1 / P2.1**:
  `harness/agent_loop.py`'s unsandboxed bash reach - deleting it turns the one
  registered known-failing pin green with no other edit anywhere. (b) **T1**:
  rename `harness/test_config.py`; it is a production module whose two
  `test_*`-named helpers make it look like a suite. (c) **T5 / P0**: register
  the `trust-ladder` suite in `evals/run.py`, or state that the Trust Ladder is
  deliberately pytest-only. (d) **T5 / release**: `litellm==1.74.9` is one
  release below its own documented minimum (`VEX-ADV-0001`, fixed 1.74.10) and
  is the single ACTIVE security blocker the G0 gate measures. (e) **T4**:
  `demo/test_product_docs.py::test_command_reference_covers_current_registry`
  is red on 20+ unregistered slash commands - a real docs-drift failure.

---

## 2026-10-02 (P1/W1, T5 - contracts, memory, evals, CI)

**The `trust-ladder` suite NOW EXISTS.** `evals/run.py:1428` registers
`trust-ladder` as a fifth `--suite` choice and `_run_trust_ladder` (at
`evals/run.py:1423`) publishes its report to
`logs/evals/<run-id>/trust_ladder_report.json`. This closes
`evals/gates/P0.py:816`'s `trust_ladder_suite` rung, which had been
`not_implemented` since P0 and is the third open item in the P0 handoff
above. The suite is a SUITE rather than a new top-level command, per the P0
brief's own instruction.

### NEW MODULE BOUNDARY: `evals/trust_ladder.py` + `evals/trust_ladder_rungs.py`

Two modules, deliberately split. The scorecard is the first; the machinery
under three of its rungs is the second, so each probe can be driven by a test
with a **deliberately broken** input.

| symbol | signature / role |
|---|---|
| `evals.trust_ladder.STATUSES` | `("pass", "fail", "blocked", "not_implemented")`. Closed; `skip` and six near-synonyms are **refused at construction** by `_status`, not merely discouraged. |
| `evals.trust_ladder.Rung` | `number, guarantee, status, detail, owner, evidence, mode, blocking, measured`. `mode` is `"measured"` (this run executed the check) or `"carried"` (the row points at another gate). A carried rung is a **pointer**, not a number, and says so. |
| `evals.trust_ladder.ladder_report` | `ladder_report(root, *, run_measured=True, measured_only=False, probe8=None, probe9=None, probe10=None, probe8_kw=None, probe9_kw=None, probe10_kw=None) -> dict`. Emits `verdict` in `MEASURED_RED` / `MEASURED_GREEN` / `PARTIAL`, plus `measured_rungs`, `carried_rungs`, `blocking_failures` and `what_this_does_not_establish`. |
| `evals.trust_ladder.NOT_ESTABLISHED` | The limits, carried in the JSON **and** printed by the CLI. Non-empty is asserted by a test. |
| `evals.trust_ladder_rungs.Timing` | One measured duration series. `.window`, `.named_stat`, `.headline`, `.to_dict()`, `.describe()`. **A window under 20 samples is labelled a `median` and its percentile is WITHHELD**; an unmeasurable metric carries `available: false` and **no `value` key at all**, so a consumer reading `value` without reading `available` gets a `KeyError`. |
| `evals.trust_ladder_rungs.Rung8Result` | `.derive_failures()` is the **single authority** for "does rung #8 fail"; the probe and the test fixtures both call it. |
| `evals.trust_ladder_rungs.Rung9Result` | `.derive_failures()` likewise. Carries `attribution` (measured per-phase) and `walk_hit_cap`. |
| `evals.trust_ladder_rungs.Rung10Result` | `.derive_failures()` likewise, across the four named links `journal_row -> traceview_reconstruct -> runview_projection -> tui_card`. |
| `evals.trust_ladder_rungs.probe_rung8/9/10` | The real probes. `probe_rung8` takes `run_loop=` and `required_turn_cap=`; `probe_rung9` takes `run_retrieval=` and `measure_frame=`. |
| `evals.trust_ladder_rungs.attribute_retrieval` | Per-phase `cProfile` attribution with a `dominant_phase` **only when one phase exceeds 50%**, and a `graph_rebuilt` flag so a warm-run attribution is never quoted as a cold one. |

**Why `derive_failures` exists as a method and not inline in the probe:** the
first version had the rule inline and the test fixtures setting
`failures=[]`, and **four of the five demonstrated rung-#8 breaks passed
green**. A fixture that writes its own verdict list tests the fixture.

### NEW MODULE BOUNDARY: `evals/ci_lanes.py`

The six labelled lanes, as **data**, so a lane list is not a claim.

| lane | budget | docker | network | blocking | workflow#job |
|---|---|---|---|---|---|
| `smoke` | 1 min | no | no | yes | `ci-lanes.yml#smoke` |
| `trust-ladder` | 5 min | no | no | yes | `ci-lanes.yml#trust-ladder` |
| `host-only` | 15 min | no | no | **no** | `ci-lanes.yml#host-only` |
| `docker` | 30 min | yes | yes | yes | `ci-lanes.yml#docker` |
| `windows` | 45 min | no | no | yes | `windows-dockerfree-ci.yml#windows-dockerfree` |
| `nightly` | 200 min | yes | yes | **no** | `nightly-quality.yml#live-quality` |

`evals.ci_lanes.check_workflows()` and
`evals.ci_lanes.registry_gate_present()` are the two checkers;
`python -m evals.ci_lanes` exits 2 on any disagreement.

**CONTRACT, and it is a new one: the known-failing registry gates EVERY
lane.** `REGISTRY_COMMAND = ("python", "-m", "tests.known_failing_pins")` runs
before each lane's first test invocation. This is additive — no existing
contract changes shape — but a terminal adding a workflow must now either
consult the registry or be recorded as a lane.

### `memory/code_graph.py` — `_caller_id` is now indexed on BOTH branches

`_resolve_calls` resolves each call site's enclosing symbol through
`_caller_id`. The R2-09 optimisation indexed only the *symbol-level* branch and
left `if caller_qualified == module:` scanning every node, so a repository
with one module-level call site per file paid O(files x nodes).

**New public-ish surface:** `memory.code_graph.CallerIndex` (a `__slots__`
class with `by_qualified: Dict[str, Dict[str, List[str]]]` and
`module_by_file: Dict[str, str]`), `memory.code_graph.NODE_KIND_PREFIXES`
(`("func:", "method:", "class:")`, pinned because a reorder changes which
candidate wins), and `_node_kind_prefix`. `_qualified_index(graph)` now
**returns a `CallerIndex`** rather than a plain dict — a caller passing the
old dict shape still works, with a documented loss of the module-branch
speedup.

**Edges are unchanged**: the candidate list, its order, and the `caller_file`
preference are byte-identical to the linear scan
`_caller_id_reference` performs, and that oracle is retained so the equivalence
is TESTED. Measured: `_caller_id` 34.7 s -> 1.1 s; forced full rebuild 88.7 s
-> 24.6 s; 55,436,109 `str.startswith` calls eliminated.

**The reason it survived a pin, which is the part worth copying:** the
existing pin compared 4 call sites against a 4-file, 8-node fixture, where an
O(nodes)-per-call-site scan is free. `tests/test_code_graph_caller_index.py`
raises the fixture to >= 40 nodes and >= 12 call sites, exercises the
module-level branch explicitly, and adds a **scaling** test — because a
correctness test cannot catch a complexity regression.

### `scripts/provenance_report.py` — the BINARY provenance shape

A binary is declared in **`provenance/binaries.json`**, not in a header,
because a binary has no header. `BINARY_REQUIRED_FIELDS` is
`(path, upstream, version, sha256, platform, licence, beats-ours-because)` —
the source set **minus `upstream-commit`** (a release archive is not a
checkout) **plus three**. The scanner **re-hashes the file on disk** and
reports `mismatched` separately from `incomplete`, because a digest that does
not match is the supply-chain case and not a paperwork one.

`fetched_binaries` is a **second section in the same file** for binaries
downloaded at run time. It does **not** require `sha256` — there are no bytes
in the tree to re-hash, and requiring one would push the author to record a
digest they cannot verify. It requires `digest-source`, and
`digest-unpinned-reason` when that source is not a digest committed here.

`EXCLUDED_DIRS` gained `.next`/`out`/`.svelte-kit`/`.nuxt`/`.parcel-cache`/
`.turbo` **because the binary lane reported two Next.js build-output `.wasm`
files as undeclared vendored binaries**. A test pins the reason's continued
presence in the file, because an exclusion with no recorded cause is an
unexplained hole.

### `NOTICE` (new, repository root)

Records ripgrep (MIT, of "MIT OR Unlicense") and `fd` (MIT, of "MIT OR
Apache-2.0") with the upstream copyright lines, states that **nothing from
Aider is copied into this tree as of this notice**, and states the open gap:
the `rg`/`fd` **versions** are pinned in `harness/search_engine.py` and the
**digests are not committed anywhere in this repository**.

### Cross-terminal requests

1. **T1 / P1.1** — emit the constraint re-injection and the turn-cap approach
   receipt from `harness/agent_loop_step.py`. Both mechanisms already exist
   (`harness/core.py:3447` and `harness/turn_caps.py`); neither reaches the
   agent loop's journal. Rung #8 is `fail` on exactly these two.
2. **T1 / P2.1** — commit the eight `rg`/`fd` SHA-256 digests into
   `provenance/binaries.json` (or into `harness/search_engine.py` and say
   which is the authority). Verifying against a *fetched* sidecar is not the
   same as pinning the digest.
3. **T4 / P1.4** — four rung-#10 findings, all in `cli/runview.py` and
   `cli/tui_components.py`: carry `module`/`source` through the projection;
   surface `retrieval_truncated` (`render_truncation_note` has no call site);
   give the card the `available` vocabulary `briefing_lines` already has; and
   stop rendering `$0.000000` for a run with `cost_known=False`.
4. **T5 / next round** — `evals.gates` is an orphan module (nothing imports
   it; it is reached only as `python -m evals.gates.P0`). Either record it in
   `tests/test_module_reachability.py::RECORDED_UNREACHED` with a reason, or
   give it a real importer.
5. **T5 / P0 handoff item (c) is now closed** — the `trust-ladder` suite is
   registered, so `evals/gates/P0.py`'s `trust_ladder_suite` rung stops being
   `not_implemented` the next time G0 runs.

**NO LIVE-PROVIDER LANE RAN AND NONE IS CLAIMED.** No live provider is
reachable (`ServiceUnavailableError: No available channel`, 3/3), so **every
model call in the Trust Ladder is a scripted double and no result in
`docs/phase1-daily-usable.md` is a claim about model quality.**
