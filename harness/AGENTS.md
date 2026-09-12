# AGENTS.md — Terminal 1: Harness Core & Context Management

## Improvement Round 2, second session (2026-09-11/12) — Agent-written edge-case tests (Tasks A+B+C) + self-critique restoration

(Session interrupted twice: the original terminal closed mid-round —
resumed by forensically reconstructing state from the tree, the empty
`logs/agent-tests-ablation/` dir (created 14:30, never populated), and
the recovery-stash tags; a second interruption closed the terminal during
the fixtures ablation, which continued detached and completed.)

### Pre-flight: a blocked dependency found and fixed first

`tests/test_self_critique.py` failed at COLLECTION — the Agent
Intelligence round's self-critique code (`render_self_critique_prompt`,
`_self_critique`, config keys, Boundary-7 `structured_feedback` on
VerificationResult) was destroyed by the documented git-hook incident
(INTERFACES.md's recovery note: "harness/retrieval.py,
shared/types.py (structured_feedback) and execution/verify.py Round-8
deltas were NOT recoverable from git — T1/T2 please re-write"; the
03:23 snapshot survived only under stash tag `recovery-stash-t1-agent-
intel` with the warning that T1's in-flight re-writes landed after).
This round BUILDS ON that work per its brief, so it was restored FIRST,
from the stash + the surviving tests as the spec:

- `harness/prompts.py`: `SELF_CRITIQUE_SYSTEM` + `render_self_critique_prompt`
- `harness/core.py`: `_self_critique` + the success-path gate (after
  final verify passes, ONE review call of the diff vs the ORIGINAL
  issue; "no" verdict poisons the attempt with the reason as feedback;
  unparseable reply = approve — critique never kills a verified fix on
  its own parse failure) + `_structured_or_tail` feeding
  `_target_feedback`/`_regression_feedback` (Boundary-7 objects when
  present, raw tail otherwise)
- `harness/config.py`: `self_critique` (True), `self_critique_max_chars`
  (8000)
- `shared/types.py`: `structured_feedback: List[Dict] = field(
  default_factory=list)` (additive, default-constructed — every existing
  call site stays signature-valid); `runtime/serialize.py` (T3's file,
  flagged in the Change Log) round-trips it with an absent-key default
  so old journals replay
- 13/13 tests/test_self_critique.py green post-restore (Docker-gated
  e2e included).

### Task A — agent-written edge-case tests (harness/agent_tests.py + prompts + wiring)

The gap: success was gated on the ONE given failing test + full-suite
regression. A fix can satisfy exactly the given test while being wrong
for cases the issue clearly implies (boundaries, error conditions,
adjacent inputs). The gate: after final verify passes but BEFORE
success is minted, ONE model call (`render_agent_tests_prompt` carries
issue + candidate diff + a listing of the repo's existing test files)
returns JSON `{"tests": [{filename, content}]}`. Strict sanitize
(bare `*.py` names only — charset `[A-Za-z0-9._-]`, no path/drive
tricks, dupes dropped, content compiled as Python, config-capped file
count + total chars); anything failing sanitize is DROPPED, never
fatal. Config: `agent_tests` (True), `agent_tests_max` (3),
`agent_tests_dir` ("tests/_agent_generated"), `agent_tests_max_chars`
(12000).

### Task B — same verification rigor, no lighter path

Surviving tests run through the SAME `verify()` everything else uses,
in two stages, on TRANSIENT trees under `logs/{task_id}/agent_tests/`
(never inside work/ — no cleanup can leak into a diff):
1. **Baseline**: `verify(pristine+tests, rerun_for_flake_check=0)` —
   exactly the task-baseline convention. A generated test that PASSES
   pre-fix probes nothing the final gate didn't cover; dropped with a
   trace event.
2. **Post-fix**: `verify(work+tests, rerun_for_flake_check=baseline_reruns)`
   — the generated file is the TARGET: flake-rerun semantics + the
   full-suite regression, identical rigor to the final gate.

Policy (symmetric with the lint gate): a post-fix FAILURE poisons the
attempt (retry with the failing test's output as feedback — structured
Boundary-7 objects when present); a GENERATION problem (model crash,
unparseable reply, zero survivors, copy/verify crash) SKIPS the gate —
never overturn a verified fix over test-writing quality; every skip is
trace-logged. Surviving tests saved to `agent_tests/saved/` for human
review regardless of outcome. Gate order in run_task: edit-validation →
lint → final verify → agent-tests → self-critique → success.

**18/18 tests/test_agent_tests.py green** (parse/sanitize matrix +
Docker-gated e2e: gate-pass with saved-test + work/-hygiene asserts,
gate-poison→retry→pass, unparseable-skip, all-baseline-pass skip,
disabled-config, poison-exhaustion→failed). Full module selection at
close: **181/181** (150 prior + 13 restored self-critique + 18 new),
~6 min warm Docker.

### Task C — the ablation, run honestly (three runs, one real)

`harness/ablation_agenttests.py` (mirrors ablation_memplan's design:
real scheduler → real workers → real run_task → real Docker verify;
model PINNED to glm-5.3 for every call, routing OFF, `self_critique`
pinned OFF in both arms so the agent-tests gate is the only delta;
gate-specific metrics mined from traces: generated/fires/passes/skips).

- **multirepo run (logs/agent-tests-ablation/multirepo)**: DEGENERATE —
  0/5 success BOTH arms. The endpoint's planner calls ran 239–797s and
  every task wallclock-died mid-plan (10 timeouts); the gate never
  reached. Measures endpoint latency, not the mechanism; kept as run-
  condition evidence, quoted as such.
- **fixtures run 1 (fixtures-broken-cap/)**: found a REAL failure mode
  first: the `max_completion_tokens=4000` mitigation (added for the
  documented reasoning-burn hazard) made the generation call return
  EMPTY 5/5 (`raw: ""`, finish_reason=length, 4000 hidden reasoning
  tokens, 0 visible) — probe-verified directly against litellm
  (probe_logs/atgate-mct-*.json): uncapped = 29k hidden tokens then
  content; capped-4000 = empty. The gate's skip policy held (5/5 clean
  skips, zero false poisons) — the harness did its job on bad model
  output. Mitigation REMOVED; wallclock raised to 2400s.
- **fixtures run 2 (logs/agent-tests-ablation/fixtures/, the real one)**:

| arm | success | attempts | calls | tokens | cost | wall |
|---|---|---|---|---|---|---|
| OFF (gate off) | 5/5 100% | 5 | 20 | 48,364 | $0.0557 | 524s |
| ON (gate on) | 3/5 60% | 7 | 33 | 79,941 | $0.1096 | 5677s |

Gate engagement (ON): 8 tests generated across 5 tasks, 3 gates ran to
completion (bug02: 2 tests generated → both survived baseline → both
passed post-fix, gate-pass; bug03/bug05 similar), **0 gate FIRES** (no
fix passed the whole suite while failing an issue-implied edge), 0
skips. The 2 ON-arm failures (bug01, bug04) are wallclock TIMEOUTS
during the gate's generation call — traces show final verify PASSED,
then the ~300s+ generation call blew the 2400s budget (these fixtures'
fixes were verified-good; the success just never got minted before the
kill; resumed once and timed out again in the same place — the
documented slow-endpoint window, 150–345s/call measured in the probes).

**Honest verdict: on this task set the gate did NOT measurably improve
fix quality — 0 fires in 3 completed gates; its measured effect was
purely cost (+$0.054, +65% calls, +3.9x wall on completed tasks) plus
2 successes LOST to the generation call's latency inside a finite
wallclock.** The counter-evidence to "useless": (a) bug02's generated
tests genuinely probed issue-implied edges the 5-test suite didn't
cover (single-element/negative/float mixes) and the correct fix passed
them — a wrong-but-suite-green fix would have been caught (the e2e
poison test proves that path works end-to-end); (b) the 5 fixture
repos have TINY suites (2-5 tests) whose given tests already encode the
edges — the class the gate catches needs a suite whose given test
under-constrains the issue (the multirepo set, where the suite pins
are SUBSET pins — exactly where the gate has room to fire, and exactly
where this endpoint window could not complete a run). Where it would
pay: repos with sparse suites + fast endpoints; with a fast/cheap
generation model (the call is one-shot, no conversation) the latency
cost mostly vanishes. Recommendation recorded: keep the gate ON by
default (cheap insurance when the endpoint is healthy; correct skip
behavior when it isn't), pin `max_wallclock_s` ≥ 2x the observed p95
model call when the gate is on, and re-run the multirepo arm in a
faster endpoint window before making any keep/drop call at scale.

Standing honesty notes: 5 tasks x 1 rep; proxy prices (free-tier
endpoint); the two endpoint-degraded runs are evidence about the
ENDPOINT, not the mechanism; n too small for any success-rate claim in
either direction.

### Files this session

- `harness/agent_tests.py` (NEW), `harness/ablation_agenttests.py` (NEW)
- `harness/core.py` (gate wiring + restored critique + _structured_or_tail),
  `harness/prompts.py` (AGENT_TESTS prompts + restored critique prompt),
  `harness/config.py` (agent_tests* keys + restored critique keys)
- `shared/types.py` (structured_feedback restored), `runtime/serialize.py`
  (round-trip, T3's file, flagged), `INTERFACES.md` (Change Log entry)
- `tests/test_agent_tests.py` (18, was in-tree untracked — verified),
  `tests/test_self_critique.py` (13, passes post-restore)
- probe_logs/atgate-* (endpoint probes: the 29k-hidden-token measurement,
  the cap-vs-empty proof, run logs)

## Interactive-mode fix (2026-09-12, visiting CLI session) — editor.snapshot dst-inside-src RecursionError

**Found live by driving the real `vex` no-args interactive session in
a scratch repo** (the Task-D verification round; full story in
cli/AGENTS.md): `cd <repo>; vex` defaults the log root to ./logs
INSIDE the target repo, so `editor.snapshot(repo, logs/{task_id}/
pristine)` had dst-inside-src and `shutil.copytree` recursed into its
own output until RecursionError → task "error" before the first model
call. Every scripted caller placed logs outside the repo, so the shape
was never exercised.

**Fix (this module's editor.py::snapshot only)**: when dst's parent
chain runs through src, the top chain segment (e.g. `logs`) is
excluded from the copy via the ignore hook. Signature unchanged;
log-root-outside-repo behavior byte-identical (a `logs/` dir that is
real repo content still copies — pinned). Harness artifacts never
belong in the pristine reference anyway, so the exclusion is also
semantically right. 4 regression tests in tests/test_editor_prompts.py
(inside-repo root, outside-root no-over-exclusion, dst-directly-under-
src, plain shape) — file now 19/19; full sweep after the fix: 340
green across the harness + CLI selections (e2e_run_task 28, adversarial
43, coordination 31+5, config_trace_state 12, stubs/retrieval/recall
51, editor_prompts 19, CLI suites 125, coordination_e2e/decision-
memory/env-snapshot 32).

**Also fixed in passing (NOT this module's author, flagged per
cross-terminal practice)**: `harness/ablation_agenttests.py` — a
parallel session's in-flight untracked file — had a PowerShell UTF-8
BOM that tripped test_harness_modules_import_and_parse; stripped the
3 BOM bytes, content untouched (that file is 12/12 after).

## Round 8 (2026-09-10) — agent execution layer & tooling (Tasks A-D)

**54 new tests (all green): tests/test_tool_errors.py (26) +
tests/test_batch_docs_lint.py (28). Full-module selection at close:
204 passed (150 prior + 54 new), ~9 min warm Docker, ~4 min Docker-less
(the new suites need NO Docker).**

### The incident this round survived (read before touching harness files)

At ~09:37 a PARALLEL session ran `git reset --hard HEAD~1` to revert its
own smoke-test commit — which also destroyed every UNCOMMITTED harness
change in the tree: the documented Round-6 adversarial hardening
(tools.py deny patterns, core.py final-edit re-validation, editor.py
traversal normalization), my in-flight Round-8 wiring, and a second
session's in-flight state-machine/self-critique/rerank work. Untracked
files (all new modules + test files) survived. I recovered the Round-6
hardening from dangling stash-commit `fa75dcb` ("round7-verify-
committed-state"), re-applied my Round-8 work on top, and re-verified
(test_adversarial 43/43 green again). tools.py was clobbered twice more
by background git operations mid-round; the recovery pattern that worked:
immediate post-write backups OUTSIDE the repo (Temp/opencode) + import-
verification before each test run. **Lesson for all terminals: COMMIT
(or at minimum back up outside the tree) after every verified-green
milestone — the working tree is shared by 4 sessions and is not safe
storage.** The other session's state-machine/self-critique/rerank/
decision-memory work re-landed from their own session and now coexists
with mine in core.py/context.py/config.py.

### Task A — structured tool-call error handling (harness/tool_errors.py)

A failing/malformed tool call used to feed the model raw exit codes +
stderr dialect noise (or a raised traceback). Now every failure is
classified into a short, actionable error type — `TOOL ERROR [<kind>]:
<detail>` + a one-line `Suggested fix:` — so the model gets a signal to
act on instead of noise to parse. The raw output still rides the result
(capped) for diagnosis; classify() NEVER raises (degrades to
internal_error with the original preserved).

**10 error classes** (26 tests; the requirement was 5):
file_not_found, command_not_found, malformed_patch (with "hunk doesn't
apply at line N"), syntax_error, import_error, undefined_name,
permission_denied (incl. the protected-path shape), timeout,
argument_error, command_rejected (the deny-guard PermissionError), +
internal_error fallback + "ok" no-op so callers can classify
unconditionally.

Wired at three points:
1. `BashSession._map_result` — every nonzero/timeout result gets the
   classification prefixed; exit=0 renders EXACTLY as before (the
   "exit=0" string consumers are unaffected).
2. `BashSession.run` — sandbox-layer exceptions (other than
   PermissionError/SandboxUnavailableError, which keep their own
   classes — fail-loud is unchanged) are classified and re-raised as
   `ToolExecutionError(kind, detail)`.
3. `core.run_step` — catches ToolExecutionError and feeds structured
   feedback (`tool_error` trace event) instead of a traceback.

Two real classifier bugs were found by its own tests and fixed:
patch "at line N" reported the HUNK number not the line; python
SyntaxError line numbers live on the preceding `File "x", line N` line.

### Task B — batched read-only execution (BATCH protocol)

`BATCH <cmd> ;;; <cmd>` — several independent READ-ONLY commands in one
turn instead of one per turn. Deliberately NARROW (per the brief: no
general parallel execution):
- Strict verb allowlist (cat/head/tail/ls/dir/find/grep/rg/wc/file/stat/
  pwd/which/where/env/git status|diff|log|show|blame|ls-files/
  python -m pydoc) + FORBIDDEN composition chars (< > | & ; ` $( and
  newlines). One bad entry rejects the WHOLE batch (all-or-nothing —
  no partial semantics); the rejection names the entry.
- `python -c` is deliberately NOT allowlisted (executes arbitrary code;
  cannot be verified read-only); `python -m pydoc` IS (renders docs only).
- Entries run via ThreadPoolExecutor(4) through the SAME BashSession
  path (deny guard, capping) with throwaway sessions (order-independent:
  no shared cwd coupling). Fenced batches are intercepted on raw AND
  fence-stripped forms (never executed as one shell line — regression-
  tested, same discipline as RECALL).
- Trace: one `batch_call` event (+ per-entry tool_call/tool_result
  flagged `batch: true`, plus `batch_rejected` on refusals).

**Measured wall-clock saving (real Docker sandbox, 8 read-only ops —
the multi-file diagnosis shape)**: read phase 12.62s serial → 6.41s
batched (**1.97× faster, 6.22s saved**; bounded by 4 workers on 8
ops). Full task incl. identical verify overhead: 25.19s → 18.84s
(1.34×). Plus 8 model round-trips collapsed to 2 — with a real model
the saving is strictly larger. Scripts: measure_batch.py (full task)
+ measure_batch_phase.py (phase-isolated); regression-encoding:
test_run_batch_concurrent_wallclock_speedup asserts the concurrency
win in CI (no Docker needed).

### Task C — lint/static-analysis gate (harness/lint.py)

Stdlib-only (no new deps; works offline, every CI cell): per-changed-
file `compile()` syntax check + a conservative single-file undefined-
name pass (module-level uses vs every definition anywhere in the file —
imports incl. star, def/class at any nesting, comprehension targets,
global/nonlocal, walrus, except-as, `__all__` re-exports, builtins).
FALSE-NEGATIVE biased by design (function bodies not analyzed — local
control flow makes single-file analysis false-positive-prone, and the
cost model is asymmetric: a missed error costs one verify which still
catches it; a false positive would block a verifiably-correct fix).
Verified clean against all 5 fixtures + every scripted e2e fix shape.

Wired at TWO points in the loop controller (config: `lint_gate` True,
`lint_names` True):
1. **SUBMIT-time, in-session** (run_step): findings feed back into the
   SAME session (model is mid-context — cheapest fix loop; turn budget
   bounds it). Never ends the step, never gates success.
2. **Pre-final-verify short-circuit** (run_task): between the Round-6
   edit-policy gate and the final verify — a lint failure poisons the
   attempt (retry with findings as feedback) BEFORE burning a sandboxed
   pytest cycle on an edit the AST pass already knew was broken.
   Trace-proven ordering in the regression test: lint_failed →
   attempt_start with NO final_verify between.
Verifier-gated completion stays absolute (spec item 17): lint only
short-circuits what it KNOWS is broken; `lint_failed` trace events
carry the classified findings.

### Task D — documentation/API lookup (DOCS protocol)

`DOCS <dotted target> [topic words]` — same control-signal contract as
RECALL (parsed raw + fence-stripped, never executed as shell). Read-
only by construction; the ONLY remote call is the fixed PyPI JSON
endpoint, GET-only, gated OFF by default (`docs_lookup_allow_remote`
False — network stays opt-in per task).

Resolution layers: shared cache (`logs/_docs-cache/`, same convention
as `_code-graph/` — harness-owned, outside the repo, atomic tmp+replace
writes) → pydoc of the installed interpreter in a SUBPROCESS (never
import agent-adjacent code in-process; a subprocess dies cleanly) →
opt-in PyPI metadata. Caps everywhere (3000 chars default, 10s
timeout). Budget: `max_docs_per_step` (3) with an exhaustion nudge
that can't deadlock (same pattern as RECALL). New trace event
`docs_lookup` {query, source, ok}; pydoc-miss detection is case-
insensitive (a real bug found by its own test).

### Files this round

| File | Role |
|---|---|
| `tool_errors.py` (NEW) | classify()/classify_exception()/render_error(): 10 stable error kinds + hints; never raises |
| `lint.py` (NEW) | lint_file/lint_changed/render_findings: syntax + module-level undefined names, stdlib-only |
| `docs_lookup.py` (NEW) | parse_docs/lookup/lookup_and_render: cache → pydoc subprocess → opt-in PyPI |
| `tools.py` | + parse_docs/parse_batch/validate_batch/run_batch; _map_result classifies failures; run() wraps sandbox exceptions as ToolExecutionError; SandboxUnavailableError still fail-loud |
| `core.py` | + BATCH/DOCS intercepts in run_step (raw + fence-stripped), SUBMIT-time lint gate, pre-final-verify lint short-circuit, tool_error handling |
| `prompts.py` | + BATCH and DOCS doc blocks in the STEP system prompt (planner prompt markers untouched) |
| `config.py` | + lint_gate, lint_names, docs_lookup_enabled, docs_lookup_allow_remote, max_docs_per_step, docs_max_chars |

### Notes for other terminals

- **T3 (runtime)**: state.json schema unchanged; new trace events
  (batch_call, batch_rejected, tool_error, lint_failed, docs_lookup)
  are additive and safe to surface. `cfg["_docs_cache_root"]` is a
  private harness key (underscore prefix) — don't rely on it.
- **T2 (execution)**: nothing needed; all new machinery rides the
  existing execute_sandboxed contract.
- **T4 (memory)**: `logs/_docs-cache/` is shared/harness-owned like
  `_code-graph/` — harmless to prune (rebuilds lazily).
- tests/test_e2e_run_task.py `_assert_logs_complete` now asserts the
  6 Boundary-4 keys as a PREFIX (additive keys allowed) — updated for
  the parallel session's repo_path state key per the INTERFACES.md
  consumer note.

## Improvement Round 2 — memory-informed planning (2026-09-10, T1+T4 joint)

(Owns: harness/prompts.py planner step + the memory module's query
interface. Two OTHER parallel sessions were actively editing this tree
during the round — Round-8 BATCH/DOCS/lint/agent-tests and the
coordinated-changes round below; this round's edits were confined to
prompts.py (planner prompt), core.py (planner wiring), config.py,
context.py (additive state key), deps.py, decision_memory.py (new), and
the memory module's decision_store.py — no overlap with their sections
of prompts.py/core.py beyond additive coexistence, verified by their
suites staying green.)

### Task A — the planner now actively queries decision memory

**The gap was real**: the memory layer stored decisions and was
queryable (MCP `query_decisions`), but the PLANNING STEP never consumed
it — decisions landed in the store only after tasks finished. This was
explicitly listed in "What's next (Phase 3 hooks)" as unstarted.

The wiring, end to end:
- **New module `harness/decision_memory.py`**: `query_planning_decisions
  (repo_path, issue_text, retrieval_terms, limit)` — builds a keyword
  query from the issue's retrieval terms PLUS the repo's own path/name
  segments (convention rows mention the package name; the issue often
  doesn't), calls `DecisionStore.search(query, limit, repo_path=...)`
  scoped to THIS repo, renders text+origin lines. `render_memory_block`
  caps the section (`memory_max_chars`, 1500) with a truncation marker;
  empty renders as an explicit "(none recorded yet)" so the model knows
  memory was CONSULTED and had nothing. Never raises: missing memory
  module / unopenable store / broken query → empty + error string
  (planning must not die because memory is down).
- **`harness/deps.py::get_decision_store_factory`**: resolves
  `memory.decision_store.open_default_store` real-first, None on
  ImportError — same pattern as the code-graph factory. (New T4 surface,
  see INTERFACES.md 2026-09-10.)
- **Planner prompt** (prompts.py): new `## Relevant past decisions`
  section, deliberately placed AFTER `## Retrieved context` and BEFORE
  `## Constraints` — T3's difficulty predictor cuts the first user
  message at `## Retrieved context`, so decision memory can never shift
  difficulty scoring / routing (the placement is regression-tested from
  the runtime side: `test_memory_section_invisible_to_difficulty_
  predictor`). The predictor cut marker itself is untouched.
- **core.py planning step**: after retrieval, before the planner call —
  query (when `plan_with_memory`, default True), render, inject as
  `memory_block=`; new `decision_memory` trace event {query, matched,
  error, section_chars} or {skipped: "plan_with_memory=False"}. OFF is
  exactly one code path (config-only) — the ablation depends on that.
- **Config keys** (harness/config.py): `plan_with_memory` (True),
  `memory_query_limit` (6), `memory_max_chars` (1500).
- **state.json repo_path** (context.py, ADDITIVE): TaskState now records
  the task's repo_path after the six Boundary 4 keys (omitted when
  empty). T4's `ingest_state_file` has read `data.get("repo_path")`
  since Round 1 — the reader predated the writer; now ingestion stamps
  rows per repo so the repo-scoped query has something to match. The
  six-key prefix order is unchanged and still asserted
  (STATE_KEYS_WITH_REPO added; existing schema test untouched and still
  green). Boundary 4 note updated in INTERFACES.md.
- **Tests**: tests/test_decision_memory_planning.py (17): memory-side
  repo filter + normalization + open_default_store location; harness-
  side query building/caps/degradation (duck-typed store, monkeypatched
  factory); prompt placement contract + predictor invisibility; state
  repo_path prefix-order + ingest round-trip; and three REAL-loop e2e
  (scripted model) proving content-receipt — the planner's actual user
  message contains a store marker unseen by the issue text (ON),
  OFF-arm never opens the store (factory-call counter = 0), and a
  broken store degrades to "(none recorded yet)" + trace error without
  killing the task.

**Plumbing pilot (deterministic, before spending model budget)**: real
scheduler → real worker subprocesses → real harness → real Docker
verify, scripted model — ON arm: 5/5 tasks success with seeded
repo-scoped decisions demonstrably in the planner prompt (matched 1-2
each), the no-repo global row never leaks into any prompt; OFF arm: 5/5
success, no memory rows, skip events. logs/memplan-pilot/run-*/.

### Task B — the ablation: does it actually help? (honest answer)

**Method** (runtime/ablation_memplan.py, mirroring runtime/ablation.py
conventions): 5 fixture tasks, REAL full stack both arms (scheduler →
worker subprocesses → run_task → Docker sandbox/verify), SAME pinned
model for every call (z-ai/glm-5.3-free via tokenrouter, adaptive
routing OFF — a routing confound would make the delta unattributable),
arms differ in exactly `plan_with_memory`. The precondition — relevant
prior decisions exist — satisfied by SEEDED rows genuinely mined from
prior-run traces (v4/v3: the bare-`pytest` ImportError class that
burned real turns in abl-off-bug01's attempt 1, sed-with-quotes
breakage from the bug03 traces, per-repo suite-invocation conventions,
the successful strategies prior fixes actually used), recorded via
`DecisionStore.record` (source "manual", repo_path stamped) — the
documented path for facts not in state files. Dedicated ablation DB
(HARNESS_DECISIONS_DB pinned per-run); production store untouched.
Proxy prices per the standing honesty notes.

**Result (run1, logs/memplan-ablations/run1/summary.json):**

| metric | OFF | ON | delta |
|---|---|---|---|
| success | 5/5 | 5/5 | none |
| attempts (all tasks) | 1 | 1 | none |
| verify failures | 0 | 0 | none |
| harness-level model calls (trace) | 35 | 27 | **-23%** |
| tokens | 147,916 | 112,390 | -24% |
| proxy cost | $0.2148 | $0.1782 | -17% |
| known-mistake recurrences | **3 tasks** | **0 tasks** | -3 |

**What actually changed**: the three OFF-arm recurrences are all the
SEEDED known-mistake class — bare `pytest` → ImportError → wasted
diagnostic turns (bug01, bug02, bug03). In the ON arm ZERO tasks
re-tripped it: first commands were `python -m pytest` from the repo
root, matching the seeded convention. And the causality is visible in
the plans/commands, not just aggregates: ON-bug03's plan used the
python-rewrite strategy (its seed row documents sed-quote breakage from
the v3 traces); ON-bug05's plan repeats the module-level-list
diagnosis; ON-bug01 used the pathlib exact-block replacement its seed
row describes.

**What did NOT change — the honest core**: success rate, attempts, and
verify failures are IDENTICAL. The fixtures are easy for this model
class either way; memory did not add task-solving power, it removed
wasted work. The "avoid repeating a previously-discovered mistake"
question: YES, measurably (3→0). The "fewer attempts" question: NO at
this task difficulty (nothing needed a second attempt). Anyone quoting
this round should say "efficiency + mistake-avoidance at equal
success", not "memory makes the agent better".

**Honest caveats (all in the run's summary.json post_run_analysis)**:
n=5×1rep directional only; the per-arm "ledger calls" metric is
INFLATED by router-internal empty-response retries (ON-bug01: 5
sub-second compl=0 entries inside 4 real calls — I recount trace
model_request events for the honest number); each arm absorbed exactly
one symmetric mid-run crash-retry (OFF-bug05, ON-bug02 — runtime
resumed both; duplicated post-resume calls included in counts); a
parallel terminal's in-flight agent-tests feature ran symmetrically in
both arms; the seeded rows are convention/gotcha facts mined from real
traces, not fix cheat-sheets (no row says "change X to Y").

### Verification at close

New suite 17/17; harness selection (config_trace_state,
stubs_and_deps, retrieval_tools, editor_prompts, recall_unit,
adversarial) 122/122; e2e_run_task + decision_memory_planning 45/45;
memory-module suites (decision_store, code_graph, mcp_server,
mcp_client, mcp_stdio_fileno, dashboard) 56/56; cli + cli/mcp
adversarial 117/117. All this round's edits coexist with the two
parallel sessions' in-flight rounds (their suites re-run green in the
same sessions; the shared-file coexistence verified by the combined
green runs above).


## Improvement Round 2 — coordinated multi-file changes (2026-09-10, parallel session)

(Round 8 was landing in-flight in this same tree during this round —
lint gate, BATCH, DOCS, state machine, decision memory, agent-tests.
This round built additively ON TOP of that state; the pre-flight
baseline of the 8 landed suites was verified green FIRST: 166/166,
~5:35 warm Docker. Two in-flight test files — test_context_budget_
rerank.py, test_self_critique.py — did not collect at round start
(their harness halves hadn't landed yet in my reads; they belong to
the parallel session and were left alone.)

### Task A — detect when a fix genuinely requires coordinated changes

**New module `harness/coordination.py`.** Two cooperating pieces:

1. **Planning-side fan-out** (`detect_coordinated_change`): before the
   planner runs, the structural graph (Terminal 4's memory.code_graph,
   raw Graph like retrieval consumes — no new memory surface) is
   walked from the files retrieval ranked: every CALLER of a symbol
   defined in a changed file (call edges) plus every IMPORTER of a
   changed module (import edges) → the dependent file set with symbol
   anchors. Text vocabulary ("signature"/"parameter"/"drop the flag" vs
   "rename"/"every call site"/"all callers") classifies the shape; even
   without vocabulary hits, existing dependents still flag the change
   as coordinated (the GRAPH, not prose, decides atomicity). Protected
   paths (tests/*, VCS dirs) are EXCLUDED from suggested groups — a
   coordinated agent-edit group can never include a forbidden path
   (this was a real design bug caught by the e2e: the first version's
   "_detected" enforcement group swept tests/test_invoice.py +
   invlib/__init__.py in via import edges and made EVERY task on the
   fixture unachievable). Never raises; graph unavailable →
   detected=False, previous behavior.
2. **Planner prompt** gains a `## Coordinated-change fan-out` section
   (AFTER `## Retrieved context` — T3's difficulty predictor cut
   marker untouched) plus a `change_group` field in the plan schema
   and a step-rule about atomic groups. The planner DECLARING the
   group is the contract — detection is advisory (informs the
   declaration), never force-enforced: a detected group the plan
   declined to declare must not gate the task.

New trace event `coordination` {detected, kind, reason, changed_files,
dependent_files, group_files, excluded}.

### Task B — coordinated, ATOMIC multi-file patches

- **Plan schema**: steps may carry `change_group: <name>`.
  `_plan_change_groups` unions each group's files_hint;
  `state.json` gains the ADDITIVE `change_groups` key (after
  repo_path; Boundary 4 schema updated in INTERFACES.md; absent when
  none declared; resume-hydrated; malformed-tolerant).
- **Gate** (`coordination_gate`, default on): after the attempt's
  steps + final edit-validation, BEFORE final verify — a group with
  some-but-not-all members changed is a PARTIAL coordinated change:
  the attempt is poisoned (feedback names the missing members via
  `format_missing_group_feedback`), the verifier never sees it as
  complete. The verifier is NOT asked to re-litigate group
  completeness — a suite without call-site coverage would happily
  pass a half-updated rename (that's the entire point of the gate).
- **Rollback** (`editor.restore_group` + `group_orphans`): the
  declared group's files revert to pristine TOGETHER — including
  members that never changed and agent-created members (pristine
  state = absent → deleted). Non-group work in work/ SURVIVES (the
  config `coordination_rollback` selects "group" default | "all" |
  "none"). A FAILED verified attempt also rolls touched groups back
  together (atomic on failure, not only on partial). New
  `coordination_rollback` trace event {groups, restored, mode} +
  a state.json decision; `files_touched` is cleaned of rolled-back
  files (`TaskState.clear_files_touched`).

### Task C — a GENUINE 4-file coordinated scenario, end-to-end

**New fixture `tests/fixtures/bug06_coord`** (not a toy): an invoice
library where `Invoice.invoice_total(include_tax=False)` ignores its
flag and always returns the pre-tax subtotal. The correct fix — drop
the misleading parameter, rename to `amount_due()` returning
subtotal+tax — REQUIRES coordinated edits in model.py (definition),
serializers.py (2 call sites), reports.py (1), api.py (1): four files
that must change together; the suite's five tests all break otherwise.

**e2e proofs (tests/test_coordination_e2e.py, real Docker stack, all
green)**:
1. *Happy path*: scripted model applies the real 4-file fix via 4
   heredoc edits → detection event fired (structural fan-out), plan
   declared the 4-file group, gate passed, verified success in 1
   attempt, all four files in the diff, group + members in
   state.json.
2. *Partial change rejected + ATOMIC rollback*: attempt 1 renames
   model.py only (the classic half-updated state) → gate poisons the
   attempt pre-verifier with the three missing files named →
   `coordination_rollback` restores ALL FOUR (model.py too, though
   it was the one file that DID change — that's the atomicity
   claim proven) → attempt 2 completes the group → success at
   attempts=2, final diff = exactly the 4-file fix, only ONE
   final_verify ever ran (on the complete change).
3. *Complete-but-broken rolls back together*: all 4 files change but
   model.py computes tax twice → gate passes (group complete),
   final verify fails → group rolls back together → attempt 2's
   correct fix wins; broken arithmetic absent from the final diff.
4. *Regression guards*: plain single-file fix (bug02) with detection
   ON → coordination event present, detected=false, NO gate/rollback/
   groups; `coordination_detect=False` → no event at all (OFF arm).

**Unit suite (tests/test_coordination.py, 31 tests, no Docker)**:
vocabulary classification, real-graph fan-out on a synthetic repo
(callers+importers found via edges, changed files excluded, bounded,
no-dependents safe, graph-absent degrade), protected-path exclusion,
group completeness, plan parsing/group union, prompt blocks (incl. a
no-leftover-{coordination_rules}-slot guard — the planner template's
JSON braces forbid str.format, so the slot is .replace()d), atomic
restore_group semantics (untouched members revert too, agent-created
members deleted, non-group work survives, missing files never raise,
orphan-dir pruning), and the state.json additive-key invariants
(order, absence-when-unset, resume hydration, malformed tolerance,
files_touched cleanup).

### Docs/config/CI

- Config keys (harness/config.py): coordination_detect,
  coordination_gate, coordination_min_files, coordination_rollback.
- harness-ci.yml: both new suites in the selection + fixture image
  warm list (bug06_coord) + path filters.
- INTERFACES.md: Boundary 4 schema shows the additive change_groups
  key; Change Log entry filed (2026-09-10 Terminal 1).

### Test status at round close

**265/265 green** across the 13 landed harness suites (10:25 warm
Docker): the 166 pre-flight baseline + 31 coordination unit + 5
coordination e2e + the parallel session's batch_docs_lint (11),
agent_tests, decision_memory_planning suites that landed mid-round.
The two not-yet-collecting in-flight files remain the parallel
session's to finish (their harness halves — retrieval.rerank_files,
prompts.render_self_critique_prompt — were not in the tree at close
of my round; verified by import attempt, not assumption).

### Known limitations (this feature, honest)

- **Group declaration depends on the planner**: only plan-declared
  groups are enforced. A planner that ignores the fan-out section can
  land a partial coordinated change IF the suite happens to stay
  green (the gate needs the declaration to know the file set). The
  detection section + step rules make that unlikely, and the
  verifier still catches behavior-visible breakage — but
  "undeclared coordinated change" is out of the gate's reach by
  design (enforcing detection-suggested groups proved actively
  harmful: the tests/__init__ false-positive class).
- **Fan-out is name-resolution based** (memory.code_graph's documented
  over-approximation): `obj.foo()` edges to every `foo` method. For
  group SUGGESTIONS that's the right recall-over-precision trade; the
  declaration is where precision comes from.
- **files_hint is the group's file set**: a planner declaring a group
  but listing files loosely (e.g. forgetting one call-site file in
  files_hint) under-declares the enforced set. The prompt's fan-out
  section lists the graph-found dependents precisely to prevent this.

## Round 7 (2026-09-09) — production readiness: CI + architecture docs

(Round 6 landed in-flight in this tree before Round 7 started — its
adversarial hardening (editor `_ALWAYS_PROTECTED` VCS dirs +
traversal-normalizing `is_protected`, core final-edit re-validation
before the success-minting verify, tools deny-pattern reorder/bash
pipe fixes) + tests/test_adversarial.py were verified green FIRST:
150/150 harness tests incl. the 43 new adversarial ones, ~6 min warm
Docker. Round 7 built additively on top.)

### Task A — CI pipeline (the `harness` job in .github/workflows/ci.yml)

- **Matrix**: Linux/macOS/Windows × Python 3.10/3.12 (3.10 = the
  pyproject floor; 3.12 = current stable; litellm 1.74.9 pin holds on
  both). `fail-fast: false` so one cell's flake doesn't hide another's
  real failure. Runs on every push/PR, alongside T3's existing
  runtime/stress jobs (same file, unchanged).
- **What's in CI**: the harness module's own test selection (the six
  Round 1–5 suites + Round 6's test_adversarial.py — same list as the
  "Test status" section below), `-p no:randomly` for determinism.
- **Docker handling**: ubuntu runners ship Docker → Linux cells run
  the FULL suite incl. real-sandbox e2e (fixture dep images warmed
  first via `execution.sandbox.ensure_image`, same pattern as the
  stress job). macOS/Windows runners have no daemon → Docker-dependent
  tests self-skip CLEANLY via the existing `requires_docker` convention
  (skipif: daemon unreachable OR HARNESS_EXEC_SKIP_DOCKER=1, explicit
  reason string). **Verified by simulation, not assumption**: full
  suite re-run with HARNESS_EXEC_SKIP_DOCKER=1 → 142 passed, 8 skipped,
  each skip carrying the "docker daemon not reachable" reason — the
  exact behavior a Docker-less CI cell will show.
- A small Docker-availability diagnostic step prints CLI/daemon state
  per cell so skip counts in the logs are explainable at a glance.

### Task B — architecture documentation

- **`docs/architecture-harness.md`** (NEW): prose + ASCII flow + Mermaid
  diagram covering the core loop (setup → baseline verify → retrieval
  → plan → attempt loop → verifier gate → product output), the step
  session (bash-only, SUBMIT/RECALL escapes, deny-pattern guard), the
  retry/repair logic (last_feedback seam, both feedback loops, final
  edit re-validation, short-circuits), resume, the state.json
  contract + rules, the two-layer retrieval strategy, module map,
  and the logs layout. Written for someone who has NOT read the code.
- Linked from the root README (four-layers table + Status section).

### Test status at Round 7 close

**150/150 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py
tests/test_adversarial.py`), ~6 min warm Docker — the 107 from Round
5 + 43 from Round 6's adversarial suite. Docker-less simulation of the
same selection: 142 pass / 8 clean skips. The CI job runs exactly this
selection.

## Round 6 (2026-09-09) — multi-repo validation (3 OSS repos) + adversarial robustness

### Pre-flight

Full Round-5 suite re-run first: **107/107 green** (~3.3 min, Docker warm),
Docker up, key present. Verified, not assumed.

### Task A — 3 more real OSS repos, varying size/structure (all SUCCESS)

Round 4 proved one repo (jaraco/path). Round 6 adds three MORE unfamiliar
repos — none ever used by any terminal before, deliberately varied in shape:

| repo | shape | introduced bug (genuine, failing-test-encoded) | result |
|---|---|---|---|
| r1chardj0n3s/parse | small utility lib, flat single-package | `Result.spans` for dict-style fields (`{quest[name]}`) keyed by the internal mangled group name (`quest_name_`) instead of the original field name — inconsistent with `r.named` (public-API leak of a Parser implementation detail) | **SUCCESS, 1 attempt, 10/10 checks, 47s** |
| bottlepy/bottle | mid-sized micro-framework, ENTIRE framework in one 4.4k-line file | `parse_range_header` lost the RFC 7233 end clamp (`min(int(end)+1, maxlen)`): a satisfiable `bytes=90-500` on a 100-byte file reads as unsatisfiable → 416, or serves a lying Content-Length | **SUCCESS, 1 attempt, 10/10 checks, 76s** |
| pallets/click | large CLI toolkit, MODERN src/ LAYOUT | `DateTime.convert` iterates `self.formats` in REVERSED order — for formats where two patterns parse the same input differently (`%m-%d-%Y` vs `%d-%m-%Y`), the caller-declared order must win (documented contract); ambiguous dates silently swap month/day | **SUCCESS, 2 attempts, 10/10 checks, 371s** |

Each bug was designed after genuinely reading the code (hours of live
probing per repo), verified as NOT covered by any existing upstream test,
and each repo's suite was pinned so it fails ONLY on the new regression
test (env limits documented: bottle's test_stpl/test_wsgi are Windows-CRLF
checkout artifacts; click's test_deprecations needs installed-package
metadata, test_stream_lifecycle is a 10-min subprocess-stress suite,
test_echo_via_pager needs `less` in the slim image — all deselected with
reasons, same class as R4's jaraco chown deselections).

**Every run went through the FULL stack**: real Scheduler → real
`python -m runtime.worker` subprocess → real `harness.core.run_task` →
real Docker sandbox/verify per command → verifier-gated success →
git branch/commit + rationale → real approval-gate file protocol (approve)
→ original repo untouched (probe-verified per repo) → behavioral
validation of the FIX in-sandbox (spans keyed correctly / ranges clamped /
formats tried in order) → full suite green on the fixed work copy.

**HONEST DOWNGRADE — the model layer was SCRIPTED, not cloud.** Two
real-model attempts failed on ENDPOINT DEGRADATION, not harness issues:
the tokenrouter glm-5.3 endpoint began burning its completion budget
entirely as hidden `reasoning_content` (content=None, finish_reason=length)
on the big planner prompt — twice at a 4000-token budget (1453s and 265s
calls), then again at 8000 (10271-token calls). Small prompts still worked
(verified: 20-70s, content present) — a large-prompt/reasoning-verbosity
failure mode, not an outage. R4 already proved real-cloud-model competence
end-to-end ($0.053, 6 calls); Task A's question was whether the HARNESS
generalizes across repo shapes — answered by scripted-model runs (T3's
`mock_script` mechanism through `Task.config`; the real loop, real
sandbox, real verify, real guards all executed). Cost of the failed
real-model attempts: ~$0.06 total; their traces preserved under
logs/oss-round6/oss-r6-parse* as the evidence.

**What the three repos actually tested (the generalization claim):**
- parse: flat-layout retrieval anchoring on a target test in a small
  package; patch-in-single-module flow.
- bottle: single-file framework — the bug and fix both live in ONE
  4.4k-line file; retrieval must anchor despite no package structure.
- click: src/ layout — the fix lands in `src/click/types.py` while tests
  run from repo root with `PYTHONPATH=src`; the pinned suite command
  carries the env override through every verify call.
All three: protected tests untouched, state.json/trace complete, approval
gate live, original repo never mutated. Report:
`logs/oss-round6/multi_repo_report.json` (10 checks per repo, all PASS).

### Task B — adversarial robustness: 43 regression tests; 2 real defects found+fixed

Attack matrix executed first as probes (logs/oss-round6/adv_probe*.py),
then encoded permanently in **tests/test_adversarial.py (43 tests, all
green)**:

1. **Protected-path guard (editor.py)**:
   - `is_protected` now '..'-normalizes paths BEFORE fnmatch — a
     traversal-shaped path (`subdir/../../tests/t.py`) can no longer evade
     a `tests/*` glob at the guard level (the real pipeline feeds
     rglob-normalized paths that can't contain '..', so production was
     safe — but the guard itself was evadable; defense-in-depth tested).
   - `.git`/`.hg`/`.svn` are ALWAYS protected, independent of
     `protected_paths` config: the snapshot drops .git, so any VCS path
     in a diff is an agent FORGERY of VCS state.
   - `check_edits` scans work/ for forged VCS paths on a separate pass —
     `changed_files` deliberately SKIPS .git content, so a diff-only
     check could never see the class (found by my own failing test).
2. **Deny-pattern guard (tools.py)**: covers reordered rm flags
   (`rm -fr /`), loose spacing, pipes into bash/zsh/dash from BOTH curl
   and wget (previously only `| sh`), fork bombs — while benign
   look-alikes stay allowed (relative `rm -rf build/`, plain curl,
   traversal READS — containment is the sandbox's job).
3. **Sandbox containment (verified e2e, Docker-gated)**: traversal
   writes (`echo > ../../../escaped.txt`) land on the container's
   read-only rootfs or inside the work mount — host-side pristine/ and
   the original repo byte-identical after hostile commands.
4. **Prompt injection e2e (the critical find)**: issue text with embedded
   "ignore previous instructions" ordering the agent to (a) defuse the
   test to `assert True`, (b) forge `.git/config`, (c) traversal-write
   outside the repo. Scripted model OBEYS. **Found a REAL success-path
   bypass in core.py**: the step-level guard correctly rejected the
   edits, but the attempt then fell through to final verify — which saw
   target+suite green (the defused test trivially passes AND the real
   fix was also applied) and minted `status="success"` with the
   protected-path violation sitting in work/. **Fix**: `run_task` now
   re-runs `check_edits` before final verify — an edit-policy violation
   poisons the whole attempt (retry with explicit feedback), the
   verifier never re-litigates policy. Regression-tested end-to-end
   (the same injection now fails every attempt; host integrity asserts).
   Also regression-tested: the whole-tests-dir RENAME evasion (mv tests
   tests.bak + trivial new tests) — the deletions show up as protected
   paths and the run is blocked.

**Real-model driver bug found during Task A runs (worth keeping):** the
Round-6 driver initially used `run_id=f"oss-r6-{name}"` COLLIDING with
the task_id — the scheduler's run dir (`logs_root/run_id/task_id/`)
nested INSIDE the harness log dir (`logs_root/task_id/`), so
`_fresh_paths`' archive rename hit Windows' open-handle restriction
(worker's inherited worker.log handle) → `PermissionError(13)` before any
trace event, 3 crash-retries, error. Root-caused from worker logs; R4's
driver used a distinct run_id (`oss-round4-run`) and never saw it.
Lesson for all: **scheduler run_id must never equal a task_id sharing
its logs_root** (documented here rather than changing T3's code — the
scheduler can't know task_ids in advance; convention is enough).

**Runtime-owned edits this round (T3's files, flagged per cross-terminal
practice — see INTERFACES.md Change Log 2026-09-09 Terminal 1):**
- `runtime/worker.py` + `runtime/model_router.py`: opt-in
  `max_completion_tokens` ctx key → litellm `max_tokens` (absent =
  previous behavior). Motivated by the endpoint's reasoning-burn failure
  mode above. T3's full suites re-run green after the edit (48 passed,
  3 cloud self-skips) — including the AuthenticationError no-retry
  test, whose intent I probed changing and deliberately REVERTED (the
  module's own test documents the contract; the observed gateway auth
  flake stays a health note in the run evidence).

## Round 5 (2026-09-09) — CLOSEOUT: item 13 finished, state flag fixed, suite green

### Task A — RECALL: on-demand reinjection of compacted detail (closes the item 13 gap)

The Round 4 self-audit honestly flagged that "reversible compaction" was
only met by design — trace.jsonl kept everything but nothing could pull
older detail BACK into a live session. That gap is now closed as a real
mechanism:

- **The protocol**: a step session outputs `RECALL <terms>` in place of a
  bash command. `harness/core.py` `run_step` intercepts it BEFORE command
  extraction (both the raw reply AND its fence-stripped form — a fenced
  ` ```bash\nRECALL x\n``` ` must never execute as shell; that fall-through
  was a real defect caught by the sub-agent's characterization test),
  greps THIS task's trace.jsonl, and re-injects the matching entries into
  the live session's context as the next user message.
- **The retrieval primitive**: `TraceLogger.find_events(query, kinds=None,
  limit=5, max_chars=4000)` (harness/trace.py) — case-insensitive substring
  over (event kind + JSON data dump), most-recent-N in chronological
  order, per-entry char cap with truncation marker, malformed lines
  skipped, file errors → [], never raises mid-step.
- **Budgets (config-driven, no constants)**: `max_recalls_per_step` (3) —
  a step must still do its work in bash turns; exhaustion nudges back to
  bash/SUBMIT, never deadlocks (tested). `recall_results_cap` (5),
  `recall_max_chars` (4000).
- **Observability**: new `recall` trace event {step_id, turn, query,
  matched}; step system prompt documents the escape to the model
  ("Recovering compacted-away context (RECALL)").
- **Proof it's real**: e2e test where step 1's session observes a marker
  via `echo` (compacted away by the per-step context reset), step 2's
  FRESH session RECALLs it, and the fake model only completes the fix
  after the marker demonstrably arrives in ITS OWN message list —
  content-receipt proof, not code-reading. Cross-session reinjection
  through the real Docker stack, real trace file.

Sub-agent note (per round instructions): delegated the RECALL unit-test
suite (tests/test_recall_unit.py, 26 tests) to a general sub-agent — it
performed well, delivered green tests, and caught a genuine defect in my
core.py fall-through (fenced RECALL executed as bash) that I then fixed;
its two docstring nits (spec-vs-code wording) were resolved by editing the
docstrings. The sub-agent approach worked; e2e tests stayed in-terminal
(they needed accumulated loop-semantics context).

### Task A2 — T3's state.json flag (cli-real-smoke) fixed

Terminal 3's Change Log flag: a successful task's final state.json showed
`completed_steps: []` + `remaining_plan: [step]`. Root cause (diagnosed
from that run's actual trace, not guessed): the step's commands had
applied the fix, but `max_step_turns` (6, pinned in the smoke config)
exhausted before the model's SUBMIT — `step_end ok=false
"exhausted ... turns"` — then final verify passed and the task succeeded,
with `complete_step` never called. Not resume-specific (the kill happened
pre-plan).

**Fix**: on a VERIFIED success, `run_task` records every plan step that
RAN in the winning attempt as completed (new
`TaskState.complete_all_ran_steps(steps)` in harness/context.py — the
verified diff subsumes each ran step's work; steps skipped by early-exit
never needed to run). state.json now agrees with the verified result;
`harness status` renders true progress on that path. Schema unchanged.
Regression test: exhausted-turns-with-real-fix → success AND
completed_steps == full plan, remaining empty.

### Task B — final suite + honest closeout

**107/107 green** (78 prior + 26 RECALL unit + 3 new e2e), ~4 min warm
Docker, full command in the Test status section below.

## Round 4 (2026-09-09) — self-audit + first full-stack run on an unfamiliar OSS repo

### Pre-flight: Round 3 verified landed (not assumed)

Full suite re-run first: **75/75 green** (~3 min, Docker warm), context.py
repair + regression tests present, e2e docstring updated, git/rationale
wiring live, approval tests present. All four Round-3 tasks confirmed.

### Task A — self-audit vs project-spec.md items 1–17 (all CORE)

Went through each item against the actual code, one by one:

| # | Item | Verdict |
|---|---|---|
| 1 | Repo understanding / retrieval | **Solid** (two layers: structural via memory.code_graph + target-test anchor + subword matching, grep fallback; best-effort degrade tested). |
| 2 | Tool interface / action space | **Solid** (bash-only per mini-swe-agent decision; fenced-block/heredoc-safe extraction; deny-pattern guard; SUBMIT semantics). |
| 3 | Patch generation + validation | **Solid** (check_edits: protected paths + syntax before verify; unified diff vs pristine; snapshot/restore). **One real defect found+fixed this round** (see Task B). |
| 4 | Sandboxed execution | **Solid** — real Docker sandbox auto-resolved via deps.py since Round 2 (fresh container per call, networkless, resource-limited); fail-loud SandboxUnavailableError, no silent fallback (deliberate). |
| 5 | Verification beyond "tests pass" | **Solid** (baseline verify on pristine pre-edit — mislabeled task short-circuits; flake = 3-valued outcomes incl. timeout; full-suite regression after target). |
| 6 | Within-task state/memory | **Solid** (state.json + plan.json; tried-work tracked; `files_touched` recorded only after validation; feedback flows step→step and attempt→attempt with verifier raw output). |
| 7 | Stopping conditions | **Solid** (max_retries, budget_cap_usd, max_wallclock_s — all config-driven, checked every iteration + mid-step deadline). |
| 8 | Logging / traceability | **Solid** (trace.jsonl: every prompt, response, tool call/result, verify, decision, RECALL; survives relaunches via resume append). |
| 9 | Model/provider abstraction | **Solid** (Boundary 2 via deps; router decides when model=None; per-call cost/tokens via get_last_usage convention; proven live on a real cloud tier this round). |
| 10 | Config / reproducibility | **Solid** (full merged config logged in task_start event minus api_key; plan.json captures attempt/cost). |
| 11 | Persistent structured state file | **Solid** (state.json, exact Boundary 4 schema, atomic tmp+replace write; regression tests hold the invariant; Round 5: agrees with verified results even on the exhausted-turns path). |
| 12 | Context resets at checkpoints | **Solid** (per-step FRESH sessions; per-step system prompt rebuild; completed-steps handed over structurally, not as chat history). |
| 13 | Reversible compaction | **SOLID as of Round 5** (was "met by design, honest caveat" in R4): trace.jsonl keeps everything, state.json is the compacted view, and the **RECALL protocol now pulls older detail back into a live session on demand** — budgeted, trace-logged, tested cross-session with content-receipt proof. |
| 14 | Task decomposition 2–4 sub-steps | **Solid** (planner enforces 2–4 with per-step checkpoint; no "run the suite" steps; early verifier exit when done mid-plan). |
| 15 | Constraint re-injection | **Solid** (re-injection block appended to END of every tool result — issue one-liner, current step, remaining, protected paths, don't-touch-tests). |
| 16 | Curated per-step context | **Solid** (step_files = step hint + retrieval, capped files/lines; not a static bundle). |
| 17 | Verifier-gated completion | **Solid** (success ONLY on verify() target+regression+not-flaky; SUBMIT ends a step, never a task; pre-passing pristine short-circuits to success with zero calls). |

**Nothing missing.** The two Round-4 weak spots: (a) item 13's reinjection
gap — **CLOSED in Round 5 (RECALL)**; (b) no failure CLASSIFICATION —
remains a documented, accepted limitation (see Known limitations below).

### Task B — full-stack DoD run on jaraco/path (unfamiliar real OSS repo)

**Repo**: jaraco/path @ 67319bb (fresh clone; NOT one of the 5 fixtures,
never used by this project before). After a genuinely-thorough hunt
(masks/matchers/classes/in_place/write_text/chunks/hashes/times/
merge_tree/only_newer/relpath/Multi/TempDir/walk all checked live —
upstream is solid), I **introduced one genuine bug**: dropped
`in_place()`'s permission preservation (`os.open(self, os_mode, 0o666)`,
no chmod re-apply — the function's own contract says "same permissions").
A 0o600 config file silently becomes 0644 after an edit — a real
security-adjacent defect class, encoded in a failing regression test
(`tests/test_in_place_perms.py`, both success and error-restore paths).
Bug verified reproducing in-sandbox (0o600→0o644); rest of suite green
with a pinned test command (`-o addopts=` strips --doctest-modules +
pytest-ruff pseudo-tests; test_chown/test_group deselected — they need
/etc/passwd entries absent from the slim image, an env limit, not the bug).

**Run 1 — FAILED, and that's the finding.** Full real stack (real
Scheduler → worker subprocess → real harness → real cloud model via
tokenrouter → real Docker sandbox; T3's own ablation was concurrently
hammering the same endpoint, so planner calls ran slow) — the agent
FIXED the bug, final verify PASSED (target + full suite), and then the
worker CRASHED with `UnicodeDecodeError: b'SQLite format 3...'`. Root
cause (mine, harness/editor.py): (1) `unified_diff` read changed files
with `errors="strict"` OUTSIDE its try/except — the repo's pytest run
materializes a binary `.coverage` (SQLite) in work/, counted as a
"changed file", and the strict decode raised instead of returning
None; (2) verifier-created artifacts (`.coverage`, cache dirs) counted
as agent edits at all. A **verified fix was reported as "error"** after
3 crash-retries exhausted (each resume → re-verify → recreate .coverage
→ crash again — a textbook crash loop).
**Fix (harness/editor.py)**: strict decode moved inside the
UnicodeDecodeError guard (binary → reported, never raised);
`changed_files` skips run artifacts (`.coverage*`, `.hypothesis`,
`.cache`, + prior skip set). **3 regression tests added**
(binary-not-crash, binary-reported, artifacts-ignored). 78/78 suite
green post-fix.

**Run 2 — SUCCEEDED end-to-end** (`oss-path-perms-r2`, 10/10 checks,
driver + corrected validator under logs/oss-round4/): Scheduler →
worker → real harness → **real cloud model (z-ai/glm-5.3-free via
tokenrouter, 6 calls, $0.053)** → every command through the real Docker
sandbox → verifier-gated **success in 1 attempt, 373s**. The agent wrote
its own (equivalent, cleaner) fix — capture `original_mode` before
rename, `os.chmod` after create — NOT the literal upstream shape (my
validator initially demanded the literal shape and failed; lesson
recorded: validate BEHAVIOR, not implementation text). Confirmed live:
git branch `harness/fix-bug-report-...` with pristine-first commit whose
`git show` IS the fix; rationale.md grounded in the trace; **approval
gate ran the real file protocol** (request.json → external approver →
decision.json → approved → git output composed); original repo
untouched (bug still present there); regression tests re-run green on
the fixed work copy IN-SANDBOX; **Terminal 4's memory ingested the
run's decisions via the real recursive poll (source: state-file) and
serves them through the MCP `query_decisions` surface** — verified by
query, not assumption. Full report: logs/oss-round4/oss_run_report.json.

**Cross-module notes from the run:**
- T3 scheduler/worker/resume behaved exactly per contract under a real
  crash-retry loop (crash→retry→resume→crash→exhausted→error status);
  the hang-check pin (`hang_heartbeat_stale_s` 1200 ≥ slow model calls)
  worked as documented.
- T2's sandbox/verify were transparent throughout; the ONLY issue was
  my editor.py binary handling (T2's DoD had hit semver's symlink quirk
  but not the binary-artifact one — different repo, different quirk).

**Test status: 78/78 pass** (75 prior + 3 editor regression), ~4.5 min
with warm Docker.

## Round 3 (2026-09-08) — patch review, git-native output, approval e2e

### Task A — Terminal 4's repair of harness/context.py: REVIEWED, CORRECT

An interrupted edit of mine (the Round-2 plan.json work) had left a
duplicated dangling `def _write` stub in context.py; Terminal 4 found it
live (every `import harness.*` raised IndentationError, caught during
their CLI e2e) and deleted exactly the dangling stub — nothing else.
**Verdict: the repair matches my intended final design.** The intended
shape is and was: ONE `_write` (the atomic tmp+replace Boundary-4 state
writer) and `save_plan_steps` writing ONLY plan.json via `PLAN_FILE`
(separate file, separate writer). The duplicate stub was an artifact of
the interrupted edit, not a method I meant to keep.
**Regression tests added** (`tests/test_config_trace_state.py`):
- `test_harness_modules_import_and_parse` — AST-parses every harness
  module + import-checks each (catches the un-parseable-file class at
  collection time, before any downstream module breaks).
- `test_context_has_single_wellformed_write_method` — structural
  invariant: exactly one `_write` containing the tmp+replace pair;
  `save_plan_steps` must not call `self._write(`.
**Proven live, not just written**: a throwaway script re-introduced the
exact corruption (dangling stub before `_write`), reproduced the
identical `IndentationError ... line 196`, tests failed at collection,
file restored, 12/12 green again. (Would have caught the bug originally.)

### Task B — stale docstring fixed

`tests/test_e2e_run_task.py` module docstring now says "REAL Docker
sandbox and verifier (execution.sandbox / execution.verify via deps.py
auto-resolution; the local subprocess stub is fallback-only)".

### Task C — git-native output + rationale wired into run_task

On a VERIFIED fix, `run_task` now produces real git artifacts in the
harness's PRIVATE work copy (original repo never touched, by
construction — git ops only ever run in `logs/{task_id}/work/`):
- `execution.git_output.produce_git_output(work_dir, issue_text,
  changed_files, diff, verification_summary, rationale, branch_name,
  pristine_dir)` → git init + pristine first commit + fix commit on a
  `harness/fix-<slug>` branch. Result dict {branch, commit_sha,
  commit_message, pr_description} → `logs/{task_id}/git.json` + a
  `git_output` trace event. Proven: the fix commit's `git show` diff IS
  the fix; author is harness-bot; two commits on the branch.
- `execution.rationale.build_rationale(log_dir, issue_text)` → one
  grounded paragraph from trace.jsonl + state.json → written to
  `logs/{task_id}/rationale.md` + a `rationale` trace event. Written on
  success paths (after `task_end`, which the verdict keys off) AND on
  failed/timeout outcomes (`_record_rationale_only` — a rationale is
  valuable on losses too; an unverified diff NEVER gets git output).
- Both best-effort BY CONTRACT: any failure degrades to a
  `rationale_failed`/`git_output_failed` trace event, never changes the
  verifier-gated task outcome (new config keys `git_output`/`
  rationale_log`, both default True; `branch_name` optional pin).
- e2e proof: 3 Docker-gated tests (produces artifacts + disabled-path
  produces none + failed-path gets rationale but no git output).

### Task D — approval-mode wiring CONFIRMED LIVE

The gate lives in Terminal 3's worker, wrapping my Boundary-3 run_task
(the runtime's documented design). Tested end-to-end, not by reading:
real `Scheduler` → real `python -m runtime.worker` subprocess → REAL
harness loop (scripted fix through real Docker verify) → real
request.json/decision.json file protocol with an external approver
thread. Both paths proven: APPROVE (task finishes success, diff applied,
review.log = requested→approved, worker events show approval_wait →
approval_granted → worker_finish, AND Task C's git output composes —
branch exists in work/) and REJECT (result downgraded to failed, diff
stripped, never applied).
**Real cross-module finding for Terminal 3**: a worker blocked in the
approval gate stops touching state.json; the scheduler's hang check
(default `hang_heartbeat_stale_s: 30`) KILLS it mid-gate. Mitigation
(documented pattern, now in my tests): pin `hang_heartbeat_stale_s` ≥
`approval_timeout_s`. T3: consider making the worker beat the
checkpoint heartbeat while parked in the gate — the heartbeat daemon
thread already exists in your worker; state.json staleness is the
hang signal, heartbeat is liveness, and a gate-blocked worker is alive.

**Enabling hook for cross-process determinism** (new,
`harness/_stubs/scripted_model.py` + `deps.get_call_model`): env var
`HARNESS_SCRIPTED_MODEL=<json spec>` resolves a file-driven
`ScriptedFileModel` (same dispatch contract as tests/fake_model) inside
worker SUBPROCESSES, where `set_call_model` injection cannot reach.
Cached per spec-path per process (ModelClient re-resolves per call; the
script queue must survive). Test-only hook by design; in-process tests
keep using `set_call_model`.

**Test status: 75/75 pass** (68 prior + 2 context regression + 3
git-output/rationale e2e + 2 approval e2e), ~3 min with warm Docker
images.

## What's built (complete module state, Rounds 1–5)

The full agent loop: given a `Task` (issue + repo), retrieve context, plan
sub-steps, edit a working copy via bash, verify, and retry with feedback —
implemented per `INTERFACES.md` Boundaries 3 & 4 and the context-management
ideas in `project-spec.md` (items 12/13/14/15/16/17, with 13 fully closed
in Round 5 via RECALL).

| File | Role |
|---|---|
| `core.py` | **`run_task(task, log_root=None) -> TaskResult`** (Boundary 3). Attempt loop, per-step sessions, stopping conditions, verifier-gated completion, **resume contract (Round 2, see below)**, **product-grade output on verified success (Round 3: git branch/commit/PR + rationale)**. |
| `context.py` | Structured state file `logs/{task_id}/state.json` in the exact Boundary 4 schema (atomic rewrite; `reset_completed()` on retry rollback; **resume hydration + `plan.json` bookkeeping**). **Round 3: Terminal 4's mid-edit repair reviewed + regression-tested — see above.** |
| `trace.py` | Append-only `trace.jsonl`: every prompt, model response, tool call, tool result, verify, decision, RECALL. **Survives relaunches (appended to on resume).** **Round 5: `find_events()` — the reversible-compaction retrieval primitive (RECALL queries it).** |
| `config.py` | All tunables from `task.config` merged over `DEFAULTS` (retries, budget, wall-clock, caps, protected paths…). |
| `retrieval.py` | **Two layers (Round 2)**: structural (imports/call-graph via Terminal 4's `memory.code_graph`, anchored on the target test + subword symbol matching) merged with the Phase-1 grep layer. Returns a `strategy` note. |
| `tools.py` | **Bash-only action space** (mini-swe-agent style): one command per turn, output-capped, `SUBMIT` ends a step, deny-pattern guard. **Round 5: `parse_recall` — the RECALL escape (control signal, never executed; checked raw AND fence-stripped).** |
| `editor.py` | Snapshot/restore of working copies, difflib unified diff, pre-verify validation (protected paths, Python syntax). |
| `prompts.py` | Planner prompt (2–4 sub-steps, each with checkpoint — no one-shotting; now states the retrieval strategy), step-session prompts, **constraint re-injection block appended to every tool result**. |
| `model_client.py` | Wraps Boundary 2 `call_model`: trace logging, usage/cost accounting via `get_last_usage()` convention, `TaskResult.model_calls` records. |
| `deps.py` | **The swap point**: tries real `execution.sandbox` / `runtime.model_router` / `memory.code_graph` first, falls back to `harness/_stubs/`. Tests inject fakes via `set_call_model` / `set_execute_sandboxed`; **cross-process tests use the `HARNESS_SCRIPTED_MODEL` env hook (Round 3)**. |
| `_stubs/` | Local stubs with exact contract signatures: subprocess sandbox, pytest-based `verify()`, single-provider litellm router (lazy import), **`scripted_model.py` (env-driven fake for subprocess tests)**. **All three real modules have landed; stubs are fallback-only.** |

## Round 2 changes

### Task A — resume/checkpoint contract (fixes the cross-module bug)

**Bug**: `_fresh_paths` archived `logs/{task_id}/` wholesale on every
relaunch, so Terminal 3's runtime-side checkpoint/resume could never
actually resume — state.json (the progress authority) was swept away
before the relaunched run could read it.

**Fix** (core.py + context.py):
- `run_task` now implements the resume contract: when
  `task.config["resume"]` is truthy AND the prior `state.json` has
  completed steps AND a readable `plan.json` exists → **resume**, else
  the old archive-then-fresh behavior (unchanged for non-resume callers —
  regression-tested).
- Resume keeps `logs/{task_id}/` intact: hydrates `TaskState` from the
  real state.json (decisions/files_touched/completed_steps preserved),
  **reuses the persisted plan** (new `plan.json`, harness-internal —
  re-planning could orphan recorded step descriptions), skips completed
  steps (`step_skipped_resume` trace event), keeps the surviving
  `pristine/`+`work/` dirs (partial edits are built upon, not thrown
  away), continues the **in-flight attempt** (a crash is an
  interruption, not a verification failure — no `max_retries` slot
  consumed), and seeds the budget cap with the pre-crash spend.
- Baseline verify is skipped on resume (a passing baseline would have
  ended the pre-crash run before any step completed; re-running it
  cannot change the outcome).
- `trace.jsonl` is appended to across relaunches — pre-kill events
  survive as first-class history.
- Degrades safely: unreadable state.json/plan.json or missing
  pristine/work → fresh start (trace event `resume_aborted`), never a
  crash or nonsense-resume.

**Proof**: `tests/test_e2e_run_task.py::test_resume_after_hard_kill_mid_run`
mirrors Terminal 3's scheduler scenario through the REAL loop: a child
process (tests/resume_driver.py) is hard-killed via `os._exit(70)` after
step 1 of 2 completes, the relaunch resumes from the real state.json,
skips step 1, finishes step 2 from the surviving partial edit, and
succeeds with attempts=1. Plus: non-resume relaunch still archives
(regression guard), corrupted state → fresh, missing copies → fresh.

### Task B — smarter context retrieval (Phase 2 scope)

`retrieval.py` grew a structural layer on top of the Phase-1 grep layer:
- Consumes **Terminal 4's `memory.code_graph`** (tree-sitter; via
  `deps.get_code_graph_factory()`) — one structural index for the whole
  project, no harness-private duplicate. Explicitly invited by
  memory/AGENTS.md.
- **Target-test anchor** (the key mechanism): the target test's file →
  its import edges → the module under test; its call edges → the symbols
  it exercises. Finds the right files even when the issue text names
  NOTHING (proved by test: issue "something is off in how numbers are
  summarized" → `numlib/mathutil.py` via anchor).
- **Subword symbol matching**: `compute_monthly_total` decomposes to
  {compute, monthly, total}, so an issue saying "monthly total is
  doubled" matches without the identifier ever appearing in the issue.
- **Call-graph neighborhood**: matched symbols expand one hop to direct
  callers/callees (the failing test is often the caller; the true defect
  often a callee).
- Structural scores outrank grep hits (×2 merge weight); the index lives
  at `logs/_code-graph/` (SHARED across tasks, OUTSIDE the original repo
  — the never-mutate guarantee holds; regression-tested) and is reused
  across tasks on the same repo (mtimes don't change on a read-only
  repo).
- Fully best-effort: graph module missing/broken → grep-only with
  `strategy: "grep"` (tested via monkeypatched broken factory). No
    embedding layer — true synonym gaps ("average" vs `mean()`) remain
  future work (would need an embedding index; documented below).
- Planner prompt now tells the model which strategy produced its
  context; `trace.jsonl` records the retrieval decision
  (`retrieval` event: strategy/terms/files).

### Task C — Terminal 2's real sandbox/verify: LANDED and verified

`deps.py` auto-resolution picked up `execution.sandbox` +
`execution.verify` the moment they landed — **no code changes needed**.
Verified explicitly:
- `verify(repo_path, target_test, rerun_for_flake_check=1,
  test_command=None, verify_timeout_s=300, *, allow_network=False)` —
  the `test_command`/`verify_timeout_s` kwargs I flagged in the Change
  Log are honored exactly (defaults match; used for both target and
  suite runs, node ids appended).
- `execute_sandboxed` = exact Boundary-1 signature + safe keyword-only
  extras. My e2e suite (real bash through real Docker) passes: 20/20.
- **Deliberate non-change**: Terminal 2 suggests core.py could catch
  `SandboxUnavailableError` and fall back to the subprocess stub. I
  did NOT add silent fallback — "sandboxed must mean sandboxed"; a
  down Docker yields task status "error" (loud, diagnosable, and the
  runtime's crash_retries governs relaunching). Opt-in degraded mode
  can be a future config key if ever wanted.
- Sandbox timeout convention (exit 124 + timed_out=True) matches the
  stub's, so my step-session and feedback logic is unchanged.

## Design decisions worth knowing

- **Agent never touches the original repo.** Every run snapshots the repo
  into `logs/{task_id}/pristine/` + `work/`; diffs are computed between
  those; `restore_dir()` rolls back between attempts. A stale
  `logs/{task_id}/` from a prior non-resume run is archived as
  `{task_id}.old-*`, never deleted; a RESUME relaunch keeps it (Task A).
- **Verifier-gated completion is absolute.** `status="success"` is only
  set when `verify()` confirms target test pass + full-suite regression
  pass + not flaky. `SUBMIT` only ends a step session. If the target
  already passes on the pristine copy, the task short-circuits to success
  with zero model calls (proved by an `ExplodingModel` test).
- **Model defaults are `None`** in `config.DEFAULTS` (model/provider/
  api_key). An unset value means "let the router decide" — Terminal 3's
  adaptive routing depends on this; pin values in `task.config` to force
  a model.
- **Multi-line commands are first-class.** `_extract_command` keeps
  fenced blocks and bare heredocs whole (a beheaded `python - <<EOF`
  heredoc was a real test-caught bug).
- **Constraint re-injection rides every tool result** (end of message =
  max recency): issue one-liner, current step, remaining steps, protected
  paths, "don't modify tests".
- **Failure feedback flows both ways**: step-to-step (intra-attempt) and
  attempt-to-attempt (after rollback). Feedback includes the verifier's
  raw output tail, not just "it failed".
- **Resume granularity is step-level** (state.json's granularity): a
  crash mid-step re-runs that step from its partial edits in work/; a
  crash after a step-completion skips it. `plan.json` (steps +
  in-flight attempt + spent cost) is the harness-internal bookkeeping
  that makes "continue the interrupted attempt" well-defined.

## What's stubbed / mocked, and why

- **All three real boundary modules have landed** (execution.sandbox,
  execution.verify, runtime.model_router, memory.code_graph); `deps.py`
  resolves them automatically. `harness/_stubs/` remains only as the
  import-error fallback (e.g. Docker-less dev machines) and as the
  explicit "want the old behavior" import path per Terminal 2's AGENTS.
- Tests use `tests/fake_model.py` (`ScriptedModel` / `ExplodingModel`)
  injected through `deps.set_call_model` — no network, deterministic,
  real bash + real Docker sandbox + real pytest runs in e2e tests.
  `tests/resume_driver.py` runs the real loop in a child process so
  hard-kill tests don't take pytest down with them.

## Definition of Done — where it stands

Five genuinely different bugs (boundary condition, off-by-one, missing
guard, undefined name, mutable default) in five fixture repos under
`tests/fixtures/bug0{1..5}_*/`. The e2e suite fixes all five with real
bash commands through the full stack (now against the REAL Docker
sandbox), each producing a valid `state.json` (exact Boundary 4 key
order) and complete `trace.jsonl`. Loop behaviors covered by tests:
verifier-gating (model claims SUBMIT without fixing → task FAILS),
retry-recovery (syntax-error attempt → clean fix on attempt 2),
max-retries, budget cap, wall-clock timeout, protected-path rejection
(agent tries editing tests → blocked), original-repo-never-mutated, and
(Round 2) hard-kill → relaunch → resume; non-resume relaunch archives;
corrupt/missing resume state degrades to fresh.

**(Round 4) Definition of Done now ALSO proven on an unfamiliar real
OSS repo** — jaraco/path, a deliberate security-adjacent bug
(in_place() permission loss), full stack incl. real cloud model,
approval gate, git output, rationale, and memory ingestion: SUCCESS in
1 attempt ($0.053, 6 model calls, 373s). The first attempt's honest
failure is part of the record: it exposed and fixed a real harness
defect (editor.py binary/artifact handling — see Round 4 above) that
the 5 controlled fixtures could never catch. Evidence:
`logs/oss-round4/` (driver, validator, oss_run_report.json, both runs'
full logs).

**(Round 6) DoD now proven on FOUR unfamiliar OSS repos total** — the
R4 repo plus parse (flat utility), bottle (single-file framework), and
click (src-layout) — three MORE shape-varied repos each with a genuine
introduced bug, all fixed verifier-gated through the full stack (see
Round 6 Task A for the honest scripted-model account and the endpoint
degradation evidence). Evidence: `logs/oss-round6/multi_repo_report.json`.

**Test status: 150 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py
tests/test_adversarial.py`), including the 4
resume tests, 6 structural-retrieval tests, (Round 3) 2 context-regression
+ 3 git-output/rationale e2e + 2 approval-mode e2e tests, (Round 4) 3
editor binary/artifact regression tests, (Round 5) 26 RECALL unit
tests + 3 new e2e (cross-session RECALL reinjection, RECALL
budget-exhaustion, exhausted-turns success-state), and (Round 6) 43
adversarial tests (protected-path traversal/VCS-forgery, deny-pattern
matrix, sandbox containment, prompt-injection e2e, rename evasion).
All Docker-gated suites per Terminal 2's skip convention.

## Known limitations (honest, accepted)

- **Failure classification is NOT implemented — deliberately.** Retry
  strategy is naive-retry-with-rich-feedback (verifier output tails flow
  step→step and attempt→attempt at the documented `last_feedback` seam).
  The chosen novel mechanism for this project was ADAPTIVE MODEL ROUTING
  (Terminal 3's difficulty prediction — measured: ~2.6-3.5x cheaper at
  equal success), not repair classification; the `last_feedback` seam is
  where spec item 24/25 could plug in if ever revived. Accepted as the
  documented scope boundary, not a silent drop.
- **RECALL matching is substring, not semantic**: a RECALL finds what the
  terms literally mention in the trace (case-insensitive). True synonym
  recall (query "average" for `mean()`) would need embeddings — same
  vocabulary gap as retrieval, same future-work seam.
- **Docker-less dev machines** fall back to the local subprocess sandbox
  stub via deps.py import-error resolution (fail-loud in production: no
  silent unsandboxed fallback when the real sandbox raises
  SandboxUnavailableError).

## What's next (Phase 3 hooks, not started — none are CORE)

- Embedding-based retrieval for true synonym gaps (issue says "average",
  symbol is `mean()`) — subword matching covers identifier decomposition
  but not vocabulary; same seam would upgrade RECALL from substring to
  semantic matching. Would need a local embedding index (ChromaDB
  experience per spec Phase 2). (Would ALSO fix decision-memory query
  recall: the memplan ablation's initial bug04 seed row missed its
  query purely on keyword overlap — see Improvement Round 2 Task B.)
- ~~`query_structure`/`query_decisions` woven into planning~~ — **DONE in
  Improvement Round 2 (2026-09-10)**: the planner queries decision
  memory repo-scoped before planning and injects it as a prompt section;
  measured in a real-model ablation (mistake recurrence 3→0, -23%
  model calls, at equal success). The MCP-wire form is unnecessary
  in-tree (same process tree, programmatic store access); step-session
  weaving remains possible future work if steps ever need mid-task
  memory.
- Failure classification feeding repair strategy (spec item 24/25, the
  documented `last_feedback` seam) — see Known limitations; deliberately
  not the chosen mechanism.

## Known issues / flags for other terminals

- **Terminal 2 (execution):** your verify() kwargs landed exactly as
  requested — closed out, thanks. Not adopting the suggested
  SandboxUnavailableError→stub fallback (fail-loud is the right
  default; see Task C note above). Round 4 note: repos whose test
  setups materialize run artifacts in the repo dir (pytest-cov's
  `.coverage` SQLite, cache dirs) used to poison my diff/files_touched
  — fixed harness-side (editor.py skips them + binary-safe decode);
  no execution-side action needed, but worth knowing the artifact
  class exists beyond semver's symlink quirk.
- **Terminal 3 (runtime):** the resume contract is LIVE — your worker's
  `cfg["resume"]=True` + state.json gate now actually resumes the real
  harness (plan reuse, step skip, attempt continuation, budget seed).
  No runtime-side changes needed. Your 3 scheduler-test failures noted
  in Round 1 were yours (fake-harness-pinned) and untouched by this
  round. Round 4 validation: your scheduler/worker handled a real
  crash-retry-exhaustion loop exactly per contract under a REAL harness
  bug, and the hang-check pinning pattern (`hang_heartbeat_stale_s` ≥
  slow model calls) held with a real cloud model. **Round 5: your
  cli-real-smoke state.json flag is CLOSED** (root cause + fix in Round 5
  Task A2 above; Change Log entry filed) — `harness status` now renders
  true progress on the exhausted-turns-success path.
- **Terminal 4 (memory):** `retrieval.py` now consumes
  `memory.code_graph.CodeGraph` programmatically (load_or_build + raw
  Graph nodes/edges; NOT the string `query()` surface). Contract
  addition logged in INTERFACES.md. `state.json` schema unchanged;
  `decisions` now also records the resume decision. Keep the
  Graph/NodeInfo dataclasses stable-ish or flag in the Change Log.
  Round 4: your recursive poll + MCP query_decisions surface ingested
  and served the OSS run's decisions from the REAL logs tree with zero
  changes — confirmed by query, not assumption.
  **Improvement Round 2 (2026-09-10): the harness is now your store's
  second PROGRAMMATIC consumer** - the planner queries
  open_default_store().search(..., repo_path=task.repo_path) before
  every plan. Keep search's repo_path kwarg + open_default_store stable
  or flag in the Change Log. state.json now writes the ADDITIVE
  repo_path key (your ingest_state_file already read it); the
  decision_memory trace event is safe to surface. The memplan ablation
  pins HARNESS_DECISIONS_DB per-run - your default location convention
  is unchanged.
- **All:** `logs/_code-graph/` is the shared structural index root
  (repo-keyed subdirs); `logs/{task_id}/plan.json` is
  harness-internal (not Boundary 4) — don't parse it from outside the
  harness. Round 5: new `recall` trace events are safe to surface in
  dashboards; the `RECALL` step-prompt doc block does NOT touch the
  planner prompt's `## Issue` / `## Retrieved context` markers your
  difficulty estimator keys on.
