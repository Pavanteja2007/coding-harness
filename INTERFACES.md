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
# MCP tools exposed:
#   query_structure(query: str) -> str      # code graph lookups ("what calls X")
#   query_decisions(query: str) -> str      # decision/pattern memory
#   record_decision(text: str) -> None      # called by Terminal 1 after each task
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

## Boundary 6 — Terminal 4's CLI calls into Terminal 3's scheduler and Terminal 1's harness

```python
# cli/main.py — implemented by Terminal 4
# harness fix --repo <path> --issue <text> --model <name>   -> calls harness.core.run_task directly
# harness run-benchmark --subset <name> --concurrency <n>   -> calls runtime.scheduler.run(...)
# harness status --task-id <id>                             -> reads logs/{task_id}/state.json
```

## Change Log
- 2026-09-12 (CLI session, PyPI packaging round): **Distribution name is
  `vex-harness` — the installed COMMAND stays `vex` (and the legacy
  `harness` alias). No code/contract changes; pyproject metadata +
  README install docs only.** Verified against PyPI's JSON API before
  building: `vex`, `vex-cli` (an AI CLI that itself installs a `vex`
  command — direct conflict), `vexx`, and `pyvex` are all TAKEN;
  `vex-harness` (chosen), `vex-agent-cli`, `vexcli`, `vexfix`, `vexai`,
  and `vex-code` are available. `python -m build` output
  `dist/vex_harness-0.1.0-py3-none-any.whl` + `.tar.gz` (145-file
  sdist, source packages only — no logs/fixtures swept in); wheel
  METADATA now carries readme/authors/urls/classifiers/keywords.
  Verified by CLEAN-VENV install (fresh venv, `pip install
  dist/vex_harness-0.1.0-py3-none-any.whl`): all 8 top-level packages
  import, `vex --help` / `--version` (0.1.0+source) / fix / status /
  memory / dashboard / mcp all resolve, and the offline demo entry
  point runs without a key. Publishing (twine upload) deliberately
  NOT attempted — needs the owner's PyPI account + API token.
  Install command: `pip install vex-harness` → `vex`.
- 2026-09-12 (CLI session, interactive-mode verification): **One REAL
  bug found live + fixed in harness/editor.py::snapshot; no contract
  changes.** `cd <repo>; vex` (the no-args interactive flow) defaulted
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
  re-verified live end-to-end through the real vex.exe (12/12 checks:
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
  normalized per-task event stream at `$VEX_TRACE_DIR/_trace/
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
  absent VEX_TRACE_DIR the whole layer is a zero-overhead no-op, so
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
  deltas. The vex-era working tree was recovered from
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
- 2026-09-09 (Terminal 2, Vex CLI Side Task 2): **CLI session persistence
  + slash commands + config file + plan preview.** Four additions, all
  leaning on existing backend contracts (no boundary changes; exit-code
  contract 0/1/2+130 intact). (A) `vex --continue` /
  `--resume <task_id>` / `--list-sessions`: a session index
  (logs/.vex-sessions.jsonl, recorded on completion AND interrupt) plus a
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
  cli/vexconfig.py — ~/.vex/config.toml (or $VEX_CONFIG): model, provider,
  budget_cap_usd, max_retries, plan_preview, log_verbosity, log_root;
  precedence explicit-flags > file > DEFAULTS; broken TOML warns once and
  is ignored, unknown keys pass through. (D) plan_preview (config key,
  default OFF = autonomous): renders the FIRST plan trace event (numbered
  steps + checkpoints) and prompts before the edit phase; reject cancels
  with checkpoints kept; reuses the Ctrl+C machinery, NOT a new protocol —
  the worker-level final-diff approval gate is unchanged and separate.
  NEW tests/test_cli_vex2.py (24); full CLI regression 126/126.
- 2026-09-09 (Terminal 2, Vex CLI side-task): **PROJECT RENAMED Vex —
  Boundary 6 entry point is now `vex`; interactive natural-language mode
  is the primary UX.** Coordinated with Terminal 4's Round-6 state (their
  adversarial hardening is complete and untouched: 62/62 green; only 3
  decorative output assertions in tests/test_cli.py updated to the new
  rich-render format, same intent — flagged for T4's review). Changes:
  (a) pyproject name=vex, console script `vex = "cli.main:main"`,
  `harness` kept as a working alias until docs migrate (demo/ still says
  `harness fix` — T4's module, deliberately untouched); argparse
  prog="vex". (b) NEW cli/ui.py — shared rich Console + VEX_THEME
  (amber/ember palette) applied to every command; ui.GLYPHS with
  encoding-probed ASCII fallbacks. (c) NEW cli/interactive.py — no-args
  `vex` on a TTY = plain-language session (sentence becomes the issue
  text, repo from CWD, repo/model switching, status/diff re-render);
  non-TTY no-args = usage error, never a hang. LiveMonitor tails
  trace.jsonl (the public observability surface — no run_task internals
  reached) for a live spinner + accruing cost during runs; benchmark live
  table; approval prompts render the request.json diff and write
  decision.json; Ctrl+C sweeps this process's orphaned containers via
  execution.sandbox's public reaper. (d) **Two real cross-platform bugs
  found + fixed by verification, not assumption**: `pip install -e .` was
  broken for everyone (flat-layout package discovery — explicit package
  list added; `vex.exe`/`harness.exe` now install and run on Windows,
  same flow Linux/macOS), and legacy cp1252 consoles crashed on the first
  glyph (UnicodeEncodeError in rich's LegacyWindowsTerm — GLYPHS
  fallbacks). (e) NEW tests/test_cli_vex.py (10: theme roles, no-color
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
