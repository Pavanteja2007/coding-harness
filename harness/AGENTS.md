# AGENTS.md — Terminal 1: Harness Core & Context Management

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

**Test status: 107 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py`), including the 4
resume tests, 6 structural-retrieval tests, (Round 3) 2 context-regression
+ 3 git-output/rationale e2e + 2 approval-mode e2e tests, (Round 4) 3
editor binary/artifact regression tests, and (Round 5) 26 RECALL unit
tests + 3 new e2e (cross-session RECALL reinjection, RECALL
budget-exhaustion, exhausted-turns success-state). All Docker-gated suites
per Terminal 2's skip convention.

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
  experience per spec Phase 2).
- `query_structure`/`query_decisions` MCP calls woven into step context
  (Boundary 5) — retrieval could consult decision memory for "we fixed
  something like this before".
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
- **All:** `logs/_code-graph/` is the shared structural index root
  (repo-keyed subdirs); `logs/{task_id}/plan.json` is
  harness-internal (not Boundary 4) — don't parse it from outside the
  harness. Round 5: new `recall` trace events are safe to surface in
  dashboards; the `RECALL` step-prompt doc block does NOT touch the
  planner prompt's `## Issue` / `## Retrieved context` markers your
  difficulty estimator keys on.
