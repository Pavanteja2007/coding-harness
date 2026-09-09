# runtime/ — Terminal 3: Concurrency, Reliability, Model Routing

## Round 5 (2026-09-09) — CLOSEOUT: final re-verification against the two cross-terminal closeout fixes

**Pre-conditions confirmed (not assumed):** both Round-4/5 closeout
fixes landed in the working tree BEFORE this round's re-verification —
T1's `harness/editor.py` binary/artifact handling (23:35, the
crash-loop fix from their OSS run; runs on EVERY real-mode success via
`changed_files`/`unified_diff`) and T2's `execution/verify.py`
three-valued flake outcomes (20:11, pass/fail/timeout; runs in EVERY
verify call). T1's constraint re-injection (spec item 15) was already
in place from Round 2.

### Task A — final full-scale stress re-run (the four Round-4 scenarios, unchanged methodology)

Sequential runs (by design — each is a full load profile against the
same Docker VM; concurrent scenarios would distort contention):

| scenario | checks | wall | report |
|---|---|---|---|
| fake 50 @ cap 50, 12 kills | all pass | 6.4s | logs/stress/r5-fake-50-50-12 |
| fake 50 @ cap 30, 20 kills (23 crash events for 20 kills — one natural crash absorbed) | all pass | 12.3s | logs/stress/r5-fake-50-30-20 |
| **real** 45 @ cap 45, 8 kills | all pass incl. 45/45 git.json+rationale+trace events, 8/8 plan_reused+step_skipped_resume, pre-kill traces survive | 183.9s | logs/stress/r5-real-45-45-8 |
| **real+approval** 45 @ cap 30, 8 kills, 150s parks | all pass (45/45 parked, 45/45 decisions honored, 0 gate-parked kills, parks exceeded the 120s window) | 449.6s | logs/stress/r5-real-appr-45-30-8 |

**No regressions from either closeout fix.** The real-mode scenarios
exercised both fixes live at 45-way concurrency with mid-run kills
(editor.py on every success path — 45/45 produced diffs + git output
with no binary/artifact crashes; verify.py on every target/suite
pytest run — no false-stable or false-flaky outcomes disturbed any
task). Zero hexec-* container residue after all four runs (T2's
orphan-reap machinery held under the kills).

Module test suite post-fixes: **48 passed, 3 self-skipped
(cloud-key smoke), 0 failures** (scheduler 14 + router 21 +
difficulty/approval 8 + provider smoke 5 active).

### Task B — write-up completeness audit + factual corrections

Delegated to a sub-agent (approach: USED for this round — a general
review-only agent ran the audit in parallel with the Task A stress
runs; the stress scenarios themselves ran sequentially by design,
each being a full load profile against the shared Docker VM) +
independently re-verified every flagged number against the on-disk
summary.json/ledgers/traces myself before editing. The audit verdict:
methodology/caveats complete,
v2/v3/v4 numbers all verify EXACTLY, but four factual errors were
found and are now fixed above:

1. **v1 success rates were SWAPPED** (old text: OFF 60%/ON 0%; disk:
   OFF 0% (5 errors), ON 60%) — corrected in both the consolidated
   table and the Round-2 v1 narrative. Also now quotes v1's real costs
   (OFF $0.0241, ON $0.0467 — ON cost 2x MORE, the saturation story
   quantified), and attributes the 7 rate-limit deaths correctly
   (5 OFF + 2 ON, not "ON-arm").
2. **v3's "2 expensive = the two scary tasks' planner calls" was
   wrong**: one was backoff-race (scary), the other parse-comma
   (medium-styled); the second scary task (slugify-case) was the
   run's single zero-call ERROR. Bounded false escalation stands, the
   attribution now matches the ledgers.
3. **"equal-or-better success" was backwards** (ON was equal-or-worse
   in every run) — the one-liner now says "at equal success on the
   clean runs" with the honest 94-100% vs 100% spread + v4's 100/100.
4. **v2 bug01 "the expensive model finished it" was unsupported**:
   the ledger+trace show the expensive call was #6 of 15
   (mid-attempt-1, escalated by the struggle signal right after a
   failing verify), routing then dropped back to cheap, attempt 1
   still failed, and attempt 2 finished ALL-CHEAP. Corrected in all
   three places; precise story now in Task C's caveats. (The v4
   lookup-default escalation — call #7 of 7 after 4 burned
   self-inflicted-import turns — was verified end-to-end from the
   trace: the escalated call's command ran the suite that passed.)

Also fixed: the 722s planner-call provenance (v4-smoke precursor, not
the v4 run itself — v4 maxed at 684s), v4's 3-expensive-call
attribution (backoff-race + parse-comma planner calls + the
lookup-default escalation, NOT "backoff-race + slugify-case"), and
the invalid v3-expanded caveat (archived summary holds only the ON
arm). The write-up is now closeout-complete: every number traceable
to a summary.json, no claim contradicts its own data. **No further
ablation runs needed** (Task A confirmed no regression).

### Round 5 status: CLOSEOUT COMPLETE

- All four stress scenarios: ALL CHECKS PASSED against the final
  harness (both cross-terminal closeout fixes in the loop).
- Ablation write-up: standalone-complete, numbers disk-verified,
  honest caveats intact.
- Module test suite green. No runtime-owned code changes this round
  (none needed — that is the point of a regression re-verification).
- Sub-agent approach: USED for the Task B audit (review-only,
  parallel to stress runs); stress scenarios themselves ran
  sequentially by design (shared Docker VM load profile).

## Round 4 (2026-09-08) — full-scale re-verification, self-audit, final ablation

### Task A — stress re-run against the FULLER harness (post-T1-Round-3 wiring)

T1 landed git-native output + rationale + approval-mode invocation inside
`run_task` at 16:25; my prior `real-45` stress run predated it (15:01), so
everything below was re-run against the fuller loop. New real-mode checks:
every success must have `git.json` + `rationale.md` + their trace events
(stress.py check 6). Four scenarios, ALL CHECKS PASSED each:

| scenario | checks | wall |
|---|---|---|
| fake 50 @ cap 50, 12 kills | all pass | 6.2s |
| fake 50 @ cap 30, 20 kills | all pass | 11.0s |
| **real** 45 @ cap 45, 8 kills | all pass incl. 45/45 git.json+rationale | 120.2s→re-run 412s* |
| **real+approval** 45 @ cap 30, 8 kills, 150s parks | all pass (45/45 parked, 0 gate-parked kills) | 473.1s |

- Real-mode kills are timed after ≥1 completed step (any mode now), so every
  killed task genuinely resumes mid-plan: `plan_reused`/`step_skipped_resume`
  proven per task from trace.jsonl; pre-kill trace events survive.
- *The real-45 re-run also caught one natural Docker flake (9 crash events
  for 8 kills) — budget consumed, resume, finish; the machinery handled it
  without the stress harness doing anything special.
- **Two stress-harness config lessons (documented, cost two failed runs)**:
  my first approval variant set `hang_heartbeat_stale_s=30` then 60 — both
  BELOW healthy work-phase durations: measured from the passing real-45
  traces, Docker-contended pytest steps legitimately run up to **97.7s**
  between state.json writes (p95=75s). Those runs state-stale-killed
  WORKING tasks until budgets died — while the gate exemption itself held
  the entire time (0 gate-parked kills even in the failing runs). Final
  config: 120s window / 150s park — the park outlives the window (proving
  the exemption) without breaking real work. Rule recorded: when stressing
  the gate, the stale window must sit between "healthy work gap p-max" and
  "park duration".
- The killer no longer accepts pre-progress victims in ANY mode (killing a
  task with zero completed steps only proves a fresh restart; the fake-mode
  tests cover that deterministically). First fake 50/12 run failed on
  exactly this before the change (t10/t11 were 1s old, no steps yet).

### T1's Round-3 cross-module finding — FIXED (approval gate vs hang check)

T1 reported: a worker parked in the approval gate stops touching
state.json, and the scheduler's state-stale hang check (default 30s) kills
it mid-gate. Fix (runtime-owned, both sides):

1. **Worker** (`worker.py`): sets `awaiting_approval: true` in its runtime
   checkpoint before parking in the gate; the heartbeat daemon KEEPS
   beating (it stops only after the gate). The marker is cleared in the
   FINAL checkpoint write — atomically with `status: "finished"` — not in
   a `finally`: a separate clear reopens a race where the scheduler sees
   marker=false + running + stale state + a not-yet-exited process and
   kills during teardown (found live in test r2).
2. **Scheduler** (`scheduler.py` `_check_timeouts`): the state-stale kill
   is skipped when the checkpoint says `awaiting_approval` OR
   `status=="finished"` AND the heartbeat is fresh. NOT exempt: a dead
   heartbeat (a parked worker whose process died is still dead) and the
   wall-clock cap (unbounded parks are still bounded).
3. Tests (in `tests/test_scheduler_integration.py`, all live process
   runs): gate-parked worker with a decision delayed past the stale window
   survives and succeeds (no hang_timeout in journal; marker set→cleared);
   a never-decided park is still ended by the wall-clock kill → timeout.
   Suite: 14/14 × 3 consecutive runs (timing margins: stale window ≥ 2×
   heartbeat cadence so a late beat under load isn't misread as death).
4. At scale: the real+approval scenario above — 45 overlapping parks,
   each ~30s longer than the stale window, 0 gate-parked kills.

**The old config mitigation still stands for other callers**: when a task
can legitimately run minutes between state.json writes (one long pytest),
size `hang_heartbeat_stale_s` above that (the ablation uses 1200s).

### Task B — self-audit, spec items 18/19/20/24 (verified, not assumed)

- **18 Concurrent execution (target 10-50)**: ✅ scheduler.py — process-
  per-task worker pool, FIFO queue, cap enforced by construction
  (spawn-gated). Proven at 10/30/40/45/50 concurrency from the event
  journal (`max_overlap <= cap` in every stress report above); 45-50 real
  Docker-backed harness instances ran concurrently with kills. `live_attempts()`
  gives external supervisors the live set (used by the stress killer).
- **19 Checkpoint/resume**: ✅ two-authority design (T1's state.json = what
  to skip; runtime checkpoint.json = whether to resume), resume decision
  in worker.py, per-task crash budget in the scheduler. Under load: every
  killed task in every stress scenario finished via resume (≥2 worker
  starts, later resume=true; real mode: plan reused + steps skipped +
  pre-kill trace survives). One honest gap: a task killed BEFORE any step
  completes restarts fresh (no progress to keep — correct by definition);
  crash budgets exhausted → timeout/error result, never a lost task.
- **20 Cost-aware routing infrastructure**: ✅ model_router.py — tier table
  w/ per-tier api_key/api_base (different gateways per tier, exercised),
  hint precedence (explicit > hint > default), per-call JSONL ledger with
  tokens/cost/routed_via_hint (the ablation's data source), 429
  exponential backoff + transient-flake retry, mock-provider path for
  offline tests, `get_last_usage()` for T1. Proven under load by the
  ablation below (134 real calls in the Round-4 v3 runs, both arms, full
  ledgers; v4 added another 152) and the
  scheduler-integration routing tests.
- **24(a) Adaptive routing by predicted difficulty, validated via
  ablation**: ✅ the full chain — v2 predictor (intrinsic issue signal
  from the first user message's issue portion, scaffolding-stripped, +
  struggle signal from the conversation tail) → router tier selection →
  ledger evidence. The ablation (final runs below) demonstrates per-task
  behavior: v3 — 57/59 ON-arm calls on the cheap tier, 2 expensive
  calls = 1 hard PLANNER call each on backoff-race (scary-styled) and
  parse-comma (medium-styled) — bounded false escalation, every fix
  step cheap (corrected at Round-5 closeout: earlier phrasing said
  "both scary tasks", but slugify-case was the run's zero-call error);
  v2/v4 demonstrated genuine struggle-driven escalation (v2 bug01:
  failing verify mid-attempt-1 → call #6 escalated to hard → routing
  dropped back to cheap — see the precise bug01 note in Task C's
  caveats). Honest limitation: escalation counts differ between
  free-tier endpoints' flakiness — the struggle path is real but was
  not needed by most tasks in this set.

Test suite after all Round-4 changes: **46 passed** (scheduler 14 +
router 21 + difficulty/approval 8 + provider smoke 13 pass/3 self-skip
counted separately above), 0 failures, 3 consecutive full-suite runs.

### Task C — final ablation write-up (consolidated, v1→v2→v3→v4; numbers verified vs disk at Round-5 closeout)

**Claim (resume-grade one-liner):** Built and ablated an adaptive
model-routing layer for a multi-agent coding harness: after an initial
honest-negative result, a v2 redesign (scaffolding-aware difficulty
prediction + struggle-signal escalation) cut LLM spend **2.6-3.5x**
(61-71%: $0.185→$0.053 on 16 real bug-fix tasks, $0.151→$0.058 on the
cleanest 16-task re-run) at equal success on the clean runs (ON 94-100%
vs OFF 100%; v4: 100% both arms) by routing 96-97% of model calls to a
cheap tier and escalating only when predicted difficulty or live
struggle justified it.

**Method:** paired on/off arms over the same task set, real end-to-end
runs (real harness loop, real Docker-sandboxed pytest verification, real
model calls via litellm, per-call JSONL ledgers). OFF = every call pinned
to the expensive model (the spec's baseline). ON = adaptive routing on;
the router predicts per-call difficulty (v2 heuristic: intrinsic issue
signal stripped of prompt scaffolding + struggle evidence from the
conversation tail) and routes easy/medium→cheap tier, hard→expensive.

**Results progression (all runs archived under logs/ablations/; every
number below re-verified against the run's summary.json at Round-5
closeout):**

| run | n | OFF success/cost | ON success/cost | ON cheaper | note |
|---|---|---|---|---|---|
| v1 | 5 | 0% / $0.0241 | 60% / $0.0467 | 0.5x (ON cost MORE) | **honest negative**: raw-text predictor saturated at "hard" (prompts are padded BY CONSTRUCTION) — the ON arm degenerated to always-expensive (17/17 calls on the expensive tier, ~2x OFF's cost); 7/10 tasks across BOTH arms died of RateLimitError (5 OFF + 2 ON), so even v1's 0%-vs-60% OFF-advantage was largely a rate-limit artifact, not a mechanism signal |
| v2 | 5 | 100% / $0.0528 | 100% / $0.0237 | 2.2x | redesign: intrinsic-issue-only scoring + struggle escalation; 30 cheap + 1 expensive call; the 1 escalation was genuine (bug01: failing verify mid-attempt-1 → struggle signal escalated call #6 to hard, routing then dropped back to cheap — see honest note below) |
| v3 (Round 3, fixed) | 16 | 100% / $0.1847 | 94% / $0.0532 | **3.47x** | +11 synthesized bug classes incl. 2 scary-text false-escalation probes; 57 cheap + 2 expensive calls; the 2 expensive = ONE hard PLANNER call each on backoff-race (scary-styled) and parse-comma (medium-styled) — every fix step ran cheap; the other scary task (slugify-case) was the run's single error and made ZERO calls |
| v4 (Round 4 re-run) | 16 | 100% / $0.1505 | 100% / $0.0581 | **2.59x** | cleanest run: 100% BOTH arms, 68 cheap + 3 expensive; per-style: all easy-styled stayed cheap, backoff-race + parse-comma took 1 hard planner call each, and lookup-default showed the genuine ESCALATION (7th and final call escalated after 4 failed-import turns — see below) |

**Interpretation (what the mechanism actually did):**
- ON-arm success did NOT drop from routing: v3's single failure was a
  planner-call gateway flake (empty-message BadRequestError, 0 calls
  logged, task error before any routing decision mattered; the OFF arm
  passed the same task in the same run). v4's clean re-run: 100% both
  arms — the strongest single data point in the set.
- v2's equal-success-at-lower-cost + v3/v4's scale-ups all moved in the
  spec's predicted direction ("similar success rate, much lower cost").
- False escalation is BOUNDED: scary text over a trivial bug costs at
  most 1 expensive call (the planner), never the whole task (v3+v4
  backoff-race: 1 hard planner call, all fix steps cheap).
- Escalations (cheap→expensive mid-task): v3 had 0; v2 had 1 (bug01);
  v4 had 1 (lookup-default — the cheap model burned 4 turns fighting a
  package-import error it caused itself with a `cat > pyproject.toml`,
  the struggle signal escalated the 7th call to hard, and the expensive
  call's command ran the suite that passed). The struggle path fires
  when the cheap tier genuinely can't finish, which is
  endpoint/task-load dependent — most tasks never needed it.

**Honest caveats (all in the run's summary.json too):**
- **Proxy pricing**: both endpoints are free-tier BYO routers; token
  counts are RAW measured, costs use published price rates for
  comparable model classes (cheap $0.20/$0.60, expensive $0.60/$2.20 per
  1M tok in/out). The cost DELTA is a price-model delta, not a bill.
- **Sample size**: 5 (v2) and 16 (v3/v4) tasks × 1 rep, synthesized +
  fixture bugs — directional evidence, not benchmark-grade. Success-rate
  differences at this n are noise; the cost difference (2.2-3.5x) is
  large enough to be robust to endpoint flakiness.
- **Task set**: 5 fixture bugs (T1's) + 11 synthesized single-file bugs
  (`runtime/ablation_tasks.py`, self-checked: each fails its target
  pre-fix, passes the full suite post-fix) — NOT hand-collected OSS bugs.
- **Endpoint variance**: free-tier routers vary ~2s→722s/call and flake
  (see v3's one error; v4's max expensive call: 684s); the router's
  retry path hides most of it.
- Round-3's first v3 attempt (logs/ablations/v3-expanded) is INVALID as
  a comparison: an intermediate runner bug passed bare fixture dir
  names (repo_path="bug01_wrap"), so the fixture tasks errored at
  snapshot. Fixed (absolute paths) and re-run — only the
  `v3-expanded-fixed` numbers are quoted above. (The archived
  v3-expanded summary.json holds only the ON arm — the OFF arm of that
  invalid run was superseded before landing; cite v3-expanded-fixed.)
- **v2 bug01, precisely** (Round-5 closeout re-verification): the
  expensive call was call #6 of 15 — escalated mid-attempt-1 by the
  struggle signal right after a failing step verify (routing then
  dropped back to cheap: calls 7-15 all cheap). Attempt 1 still failed
  its final verify; attempt 2 re-planned and finished the fix ALL-CHEAP.
  The earlier phrasing "the expensive model finished it" was wrong;
  the honest story is: the struggle path ESCALATED correctly under
  live failure, but the cheap model was sufficient to complete once it
  had another attempt. Cost of the escalation: 1 call.

**Phase 6 next step** (recorded, not started): re-run on SWE-bench Lite
subsets with real paid tiers, multiple reps, and report cost-at-equal-
success with confidence intervals.

### Round 4 file changes (runtime-owned)

- `runtime/worker.py`: awaiting_approval marker (set pre-gate; cleared
  atomically with status=finished — see race note above).
- `runtime/scheduler.py`: gate-aware + teardown-aware state-stale hang
  check (heartbeat still king; wall-clock still backstop).
- `runtime/stress.py`: check 6 (product-output per success), approval
  variant (`--approval`, per-gate waiter threads, 120s/150s window/park),
  pre-progress victims excluded in all modes, `_trace_kinds` helper.
- `tests/test_scheduler_integration.py`: +2 approval/hang interaction
  tests (14 total now).
- `runtime/ablation.py` + `runtime/ablation_tasks.py`: unchanged this
  round (fixture-path fix was Round 3's, 19:43) — v3 re-run validated it.

### Round 4, later session — Task B re-run (v4) + Task C (CLI verification)

**v4 ablation re-run** (`logs/ablations/v4/summary.json`, both arms in
one invocation, ts 20260908-214955): a SECOND, fully-clean 16-task run
(validating reproducibility, and this time with the fixture tasks
green in both arms — the Round-3 fixture-path fix confirmed twice now):

| arm | success | calls | tokens | cost | wall |
|---|---|---|---|---|---|
| OFF | 16/16 100% | 81 | 138,526 | $0.1505 | 2717s |
| ON | 16/16 100% | 71 | 136,436 | $0.0581 | 812s |

- 100% success BOTH arms at **39% of baseline cost (2.59× cheaper)**,
  3.3× faster wall — consistent with v3-expanded-fixed's 3.47× (same
  direction, different endpoint-load windows).
- Model mix ON: 68 cheap + 3 expensive. Per-style routing (verified
  from ledgers at Round-5 closeout): all 8 easy-styled texts stayed
  cheap (easy hints); backoff-race (scary) and parse-comma (medium)
  each took ONE hard PLANNER call then finished cheap; slugify-case
  (scary) all cheap; lookup-default showed the one genuine
  cheap→expensive ESCALATION after struggle — call #7 of 7, after 4
  burned turns fighting a self-inflicted import error (v3-fixed had 0
  escalations; v2 had 1 — the struggle path fires when the cheap tier
  genuinely can't finish, which is endpoint-load dependent).
- Endpoint health this window: the expensive tier's slowest call
  measured 722s (the single-task v4-smoke precursor's first OFF-arm
  planner call; the v4 run itself maxed at 684s on an OFF-arm bug03
  call) — free-tier variance as usual; cheap tier 3-50s/call. OFF arm
  wall reflects it.

**Ablation summary-merge fix (the Round-3 gotcha)**: running arms as
separate invocations with the same `--out` now MERGES into the
existing summary.json (arms accumulate + an `arm_runs` provenance list)
instead of clobbering. Verified with a monkeypatched scheduler two-
invocation probe: both arms + delta present after sequential `--arm
off` then `--arm on`. Both-invocations-in-one remains the default
recommendation.

### Round 4 — Task C: split-brain through the REAL CLI invocation

**Residual FAKE-path split-brain found + fixed (runtime-owned):** the
fake harness wrote state.json to `./logs/{task_id}` (repo CWD default)
while the worker read it via `state_json_path()` (pinned log_root) —
so `harness run-benchmark --log-root <custom>` + fake harness broke
resume outside the repo root. Fix: `fake_harness._state_path` now
resolves through `runtime.paths.state_json_path` (same authority as
worker/scheduler), and `_result` writes trace.jsonl into the SAME
tree. `fake_state_dir` still overrides for tests. All scheduler tests
(46) green post-change.

**Verified end-to-end via Terminal 4's CLI (`python -m cli
run-benchmark`), non-default `--log-root logs/cli-kill-resume`:**
- 3 fake-harness smoke_repo tasks @ conc 2, mid-run kill of one
  worker (stress.py killer pattern via `Scheduler.live_attempts()`):
  ALL CHECKS PASSED — killed task 2 worker starts with resume=True,
  all artifacts (state.json, {id}.runtime/ checkpoint+events,
  attempt dirs + result.json, scheduler run journal) under the
  CUSTOM root, ZERO artifacts leaked into repo ./logs.
- **REAL-harness path through the CLI** (`logs/cli-real-smoke/`): one
  smoke_repo task, real model endpoints (adaptive routing ON,
  hang_heartbeat_stale_s=300 per T4's documented requirement) →
  `success`, $0.0081, 7 cheap-tier calls (easy/medium hints, zero
  hard calls), git.json + rationale.md produced, everything under the
  custom root. Honest supervision note: the first worker was
  hang-killed at exactly 300s (state_stale) — the cheap endpoint's
  first planner call ran ~300s+ under load — then requeued and the
  relaunch finished in ~3 min. T4's 300s guidance is BORDERLINE when
  the cheap tier is loaded; 600 would be safer (the ablation uses
  1200). The cycle itself (kill → requeue → success) is the
  supervision machinery working as designed.


## Round 3 (2026-09-08, evening) — real-mode stress, expanded task set

Landed (all verified by the Round-4 sections above):

1. **`runtime/stress.py --mode real`** — stress through the REAL
   `harness.core.run_task`: real Docker bash + pytest verify + the real
   resume contract, with scripted model responses (offline). First
   passing target-scale run: **45 tasks @ conc 45, 8 simultaneous
   mid-run kills → 45/45 success, 8/8 real resumes** (asserted from
   trace.jsonl: `plan_reused` / `step_skipped_resume` / pre-kill trace
   survival), zero leaked containers
   (`logs/stress/real-45/`, plus `real-smoke/` calibration run).
2. **`runtime/mock_provider.py` — `install_script()` /
   `make_scripted_model_fn()`**: plan + per-step bash scripts from a
   plain JSON-serializable dict, so the scripted model can ride through
   `Task.config["mock_script"]` into SUBPROCESS workers (callables
   can't cross the process boundary). `runtime/worker.py` honors the
   key (see "config keys" below).
3. **`runtime/ablation_tasks.py`** — 11 synthesized buggy repos (varied
   bug classes AND varied issue-text styles: 5 easy one-liners, 4
   medium recipes, 2 deliberately SCARY texts over trivial bugs — the
   false-escalation probe). `python -m runtime.ablation_tasks --check`
   self-verifies on the host (fails pre-fix, green post-fix): 11/11.
4. **`runtime/ablation.py`** — `--tasks fixtures|extra|all`, per-arm
   escalation metrics (cheap→expensive moves), issue-style labels in
   the report, fixture paths resolved against tests/fixtures (the
   Round-3 path bug: bare "bug01_wrap" repo_path → WinError 3 at
   snapshot; verified fixed by the v4 re-run, all 5 fixtures green in
   both arms).


## Round 2 (2026-09-08) — first real ablation + stress at target scale

### The ablation (Phase 5 first data point) — REAL bugs, REAL models

Runner: `python -m runtime.ablation` (all 5 of Terminal 1's fixture bugs,
real harness + real scheduler workers + real model calls, both arms).

**v1 (honest negative, archived `logs/ablations/v1-heuristic`):** the v1
predictor scored raw message text, but harness prompts are big BY
CONSTRUCTION (system templates + injected file context) — every call
saturated at "hard", so the ON arm degenerated to always-expensive
(17/17 calls on the expensive tier, costing ~2x the OFF arm: $0.0467
vs $0.0241). Also: the expensive endpoint rate-limits at 8 req/min and
7/10 tasks died with RateLimitError (5 in OFF, 2 in ON) → even v1's
OFF 0% vs ON 60% "delta" was largely a rate-limit artifact, not a
mechanism signal (which arm "won" tells you nothing when the losing
arm is the one eating the rate limit). Both findings were fixed before
the v2 run (that's what iteration looks like — v1 was NOT tuned to
look good).

**v2 (final, `logs/ablations/v2-heuristic-{off,on-r2}`):**

| arm | success | calls | tokens | cost* | wall |
|---|---|---|---|---|---|
| OFF (always-expensive) | 5/5 100% | 17 | 38,680 | $0.0528 | 575s |
| ON (adaptive v2) | 5/5 100% | 31 | 69,615 | $0.0237 | 300s |

- **Same success rate (100% both arms) at 45% of baseline cost** — the
  spec's expected direction ("similar success rate, much lower cost").
- ON arm model mix: 30 cheap + 1 expensive calls. The one escalation was
  bug01: after a failing step verify mid-attempt-1, the predictor's
  STRUGGLE signal (failing-test output in the conversation) escalated
  call #6 to hard (9×easy → 5×medium → 1×hard across the task), then
  routing dropped back to cheap for calls 7-15. Attempt 1 still failed
  its final verify; attempt 2 finished the fix all-cheap. (Corrected
  at Round-5 closeout from the ledger + trace: the earlier "expensive
  model finished it" was wrong — the struggle ESCALATION fired
  correctly under live failure; the escalation cost 1 call and the
  cheap tier completed the task.) The mechanism did its job on a
  per-task basis, not just on average.
- *Cost caveat (honesty): endpoints are free-tier routers; tokens are raw
  measured, costs use proxy price rates for comparable model classes
  (documented in the runner + summary.json). Cost DELTA is a price-model
  delta. 5 tasks × 1 rep is directional only — Phase 5/6 should re-run on
  SWE-bench subsets with real paid tiers.
- Endpoints: cheap = qwen3.8-27b (router.bynara.id), expensive =
  z-ai/glm-5.3-free (tokenrouter). Both measured tonight: cheap tier ran
  90-240s/call under load; ablation ran at --concurrency 2 to stay under
  the expensive tier's 8 req/min limit.

### Task B — stress at real target scale (`python -m runtime.stress`)

40 tasks @ conc 40 w/ 7 simultaneous mid-run kills; 50 @ 50 w/ 12 kills;
50 @ cap 30 w/ 20 kills — **ALL checks passed each time**: every task
finished (killed ones resumed, verified via worker events: ≥2 starts,
later marked resume=True), concurrency cap held (journal-reconstructed
max_overlap ≤ cap), every kill recorded + requeued, parallel beat the
serial floor 5-10s vs 200s. Stress found + fixed one race in the STRESS
HARNESS itself (killing a just-spawned PID can race the worker's own
startup — now victims must be ≥1s old; first 50/12 run had 2 kills land
on tasks that had already finished). New public API:
`Scheduler.live_attempts()` — snapshot of running attempts for external
supervisors (the stress killer uses it; dashboards can too).

### Fixes landed this round (all test-covered)

1. **Scheduler/worker split-brain (real bug, found by the ablation):** a
   scheduler given a non-default `logs_root` read checkpoints in ITS
   tree while workers wrote theirs to `./logs/` (worker default) —
   checkpoint/resume silently broken outside the repo root, ledgers
   unreadable (ablation showed `calls=0`). Now `_spawn` pins
   `resume_dir` + `log_root` into the task config (task-config overrides
   still win), and `logs_root` is resolved absolute.
2. **Router tier endpoints:** `model_tiers` entries may carry their own
   `api_key`/`api_base` (multi-provider routing — each tier hits its own
   gateway). Also `api_base` at context level. Tests:
   `TestTierEndpointWiring` (fake-litellm captures the actual kwargs).
3. **Rate-limit + transient-error retry in the router:** 429s back off
   exponentially (default 15s base, 4 retries); transient upstream errors
   (5xx, connection errors, and the observed empty-message
   BadRequestError gateway flake that killed a planner call while the
   identical request replayed fine) get 2 short retries. Config:
   `rate_limit_retries`, `rate_limit_backoff_s`. Tests:
   `TestRateLimitRetry`. One bug caught in my own fix along the way: a
   refactor dropped `messages` from the litellm kwargs — the ablation's
   OFF arm caught it immediately ("field messages is required").
4. **v2 difficulty predictor** (`runtime/difficulty.py`): intrinsic
   signal extracted from the FIRST user message's ISSUE portion (cuts at
   `## Retrieved context` — prompt scaffolding must not count; that's
   exactly the v1 failure), plus STRUGGLE signal from the conversation
   tail (failing-test output / syntax errors / many burned turns) that
   escalates routing mid-task with zero harness changes (verifier
   feedback already rides the user messages). Calibration: the 5 fixture
   bugs → easy/medium; a stack-trace+concurrency issue → hard; struggle
   evidence escalates. Scaffold markers mirrored in one tuple
   (`_SCAFFOLD_MARKERS`) — if `harness/prompts.py` changes its section
   headers, update that tuple (T1: please keep the `## Issue` /
   `## Retrieved context` headers stable, or tell me).

### For Terminal 1 (verified, not assumed)

- **The resume contract is LIVE and verified at scale** (their Round-2
  landing + my Round-4 stress): plan reuse, completed-step skip, attempt
  continuation, budget seed — all proven under 45-way concurrency with
  kills (see Task A above). The old blocker note is closed.
- **The approval/hang finding they reported is fixed** (see Task A):
  gate-parked workers are exempt from the state-stale kill while their
  heartbeat is fresh; their "pin hang_heartbeat_stale_s >=
  approval_timeout_s" mitigation is no longer required (still fine).
- **Their HARNESS_SCRIPTED_MODEL env hook** is what real-mode stress uses
  under the hood — wait, no: stress uses the runtime's own
  mock_script/install_script path through Task.config (worker-side), not
  the env var; both exist, both test-only.
- Their 3 reported scheduler-test failures never reproduced here (see
  Round 2 below); 14/14 pass now, 3 consecutive runs.

## What's built

| Module | Status | Notes |
|---|---|---|
| `model_router.py` | **Real** | Boundary 2 `call_model`, exact INTERFACES.md signature. litellm underneath (pinned `1.74.9` for py3.10). Per-call JSONL ledger + `get_last_usage()`. **NEW: per-tier api_key/api_base, rate-limit backoff, transient-flake retry.** |
| `difficulty.py` | **Real** | **v2**: intrinsic issue signal (first-user-message issue extraction, scaffolding-stripped) + struggle escalation from conversation tail; "llm" estimator w/ heuristic fallback unchanged. |
| `scheduler.py` | **Real** | Process-per-task supervision, concurrency cap (proven at 10-50), FIFO queue, wall-clock + hang timeouts, per-task crash budget with resume-on-relaunch, run-level event journal. **NEW: `live_attempts()` public view; spawn pins resume_dir/log_root (split-brain fix).** |
| `worker.py` | **Real** | `python -m runtime.worker --task-json <path> --run-dir <path>`: loads Task, sets router context + ledger, heartbeats, runs `run_task`, approval gate, writes `result.json` + checkpoint. |
| `approval.py` | **Real** | Cross-process file protocol: worker writes `request.json` and blocks; external approver writes `decision.json`; timeout → failed. Crash-restart-safe. |
| `checkpoint.py` | **Real** | Runtime-owned resume bookkeeping under `logs/{task_id}.runtime/`: `checkpoint.json` (atomic), `heartbeat.json`, `events.jsonl`. |
| `ablation.py` | **Real** | The Phase-5 ablation runner: task sets fixtures(5)/extra(11)/all(16) × on/off arms, real stack end-to-end, per-arm success/cost/token stats from ledgers, honesty notes baked into summary.json. Final run: `logs/ablations/v3-expanded-fixed` (see Task C write-up above). |
| `ablation_tasks.py` | **Real** | The 11 synthesized Round-3 ablation repos (built at run time under the run's own out dir; `--check` self-verifies each fails pre-fix / passes post-fix on the host). Style-labeled issue texts incl. 2 scary false-escalation probes. **Round 4: `--check` hardened against a stale-`__pycache__` false verdict — several canonical fixes are byte-for-byte the same length as the bug (`upper()`→`lower()`, `order[1]`→`order[2]`), so a rewritten module could share (coarse-mtime, size) with its cached buggy bytecode and CPython would reuse the STALE pyc; check now runs with PYTHONDONTWRITEBYTECODE=1 + `-p no:cacheprovider` (11/11 across 3 consecutive runs after, was ~1 flaky BAD per 2-3 runs).** |
| `stress.py` | **Real** | Task B harness: N tasks at target concurrency with simultaneous multi-kill; asserts completion, resume proof, cap proof, journal records, product-output (real mode), approval-gate exemption (real+approval mode); standalone (`python -m runtime.stress`), NOT in the pytest suite (spawns 40-50 real workers). |
| `fake_harness.py` | **Fake (by design)** | Boundary-3-shaped `run_task` with config-driven fault injection (crash/hang/fail-at-step/resume). **Round 4: state.json + trace now resolve via `runtime.paths.state_json_path` (same pinned log_root as worker/scheduler — fixes the fake-path split-brain under a custom `--log-root`); `fake_state_dir` still overrides for tests.** |
| `mock_provider.py` | **Real (offline)** | Deterministic mock call_model back-end; zero-network routing tests. |
| `fsutil.py` / `serialize.py` / `config.py` / `paths.py` | **Real** | Windows-safe atomic IO, TaskResult↔dict, defaults + key docs, per-task path layout. |

## Resume architecture (how checkpoint/resume actually works)

Two authorities, deliberately separate:

1. **`logs/{task_id}/state.json`** — Terminal 1's structured per-step state (Boundary 4, six-field schema). The **progress authority**: `completed_steps` is what "done" means. The real harness must rewrite it per step (fake harness does).
2. **`logs/{task_id}.runtime/checkpoint.json`** — runtime's own bookkeeping (attempt number, last TaskResult, running/finished status). **Never** a duplicate of harness state; used to detect relaunches and final results. NOTE the `.runtime` sibling layout (see `runtime/paths.py`): the real harness's `core._fresh_paths` archives `logs/{task_id}/` wholesale on every relaunch, so runtime bookkeeping must live OUTSIDE that dir.

Resume decision (worker): resume ⟺ resume enabled AND state.json has completed steps AND checkpoint status ≠ finished. **state.json alone decides what to skip** — the runtime checkpoint decides *whether* a relaunch is a resume. Fault injections in the fake harness are one-shot: they disarm on ANY relaunch (checkpoint exists), otherwise a respawned worker would re-crash at the same step until the budget dies.

Scheduler kill semantics: wall-clock (attempt age > `max_wallclock_s`) and hang (state.json mtime stale > `hang_heartbeat_stale_s`, with liveness grace vs the heartbeat) both kill the worker and consume one `crash_retries` slot; exhaustion yields status `timeout`/`error` — never a raise.

## Model routing (the novel mechanism)

- **Toggle**: `task.config["adaptive_routing"]` True/False — single flag, ablation-ready. **Ablation RUN for real this round — see top of file.**
- **Tiers**: `task.config["model_tiers"] = {hint: {provider, model, api_key?, api_base?}}`; defaults in `runtime/config.py`. Per-tier keys hit different gateways (each tier can use its own endpoint+key).
- **Precedence**: explicit provider/model (call-level or config) always beats the hint; hint beats defaults.
- **Hint ingress**: caller passes `difficulty_hint`; if None and routing is on, the router predicts difficulty from the message content itself (heuristic by default). Recursion-safe for the LLM estimator.
- **Ledger**: every call appends `{ts, model, provider, prompt_tokens, completion_tokens, tokens, cost_usd, elapsed_s, routed_via_hint, difficulty_hint}` to `logs/{task_id}/runtime/model_ledger.jsonl` (when worker-driven) — this is the ablation's data source. litellm-reported cost is used when present; a price-table fallback estimates otherwise.
- **Offline testing**: `use_mock_provider: True` + `mock_responses: {model: content}` routes identically (tier selection, ledger, costs) with zero network.

## config keys the runtime reads (all via Task.config)

`concurrency` (run-level), `max_wallclock_s`*, `crash_retries`, `resume`, `resume_dir`, `approval` ("require"), `approval_timeout_s`, `hang_heartbeat_stale_s`, `adaptive_routing`, `model_tiers` (per-tier `api_key`/`api_base`), `difficulty_estimator` ("heuristic"|"llm"|"off"), `difficulty_llm`, `provider`/`model`/`api_key`/`api_base`, `use_mock_provider`, `mock_responses`, `mock_script` (scripted-harness-model spec dict; see `runtime/mock_provider.install_script` — test/offline only), `rate_limit_retries`, `rate_limit_backoff_s`.

*`max_wallclock_s` deliberately matches `harness/config.py` (same key, same meaning) — do not fork its name. `max_retries` stays the harness's verification-retry knob; the scheduler's crash budget is the separate `crash_retries`.

Keys the runtime ADDS to the dict it passes onward (workers see them; harmless if unused): `resume` (bool, resolved by the worker), plus fake-harness test keys when testing.

## litellm version pin — important

`litellm==1.100.0` (latest) **breaks on Python 3.10** (`ImportError: cannot import name 'NotRequired' from 'typing'` inside the Anthropic passthrough path). The environment runs Python 3.10.11. **Pinned `litellm==1.74.9`** — imports and calls cleanly on 3.10. Terminal 1's stub docstring already warns about this; their lazy-import pattern is the right defense and is preserved in the real router.

## What's still fake / pending integration

- **Nothing runtime-owned is stubbed anymore.** The fake harness
  (`use_fake_harness: True`) remains BY DESIGN for deterministic
  scheduler tests; the real-harness path is verified at scale (Task A).
  The Round-2 "blocking gap" (T1's resume contract) closed in their
  Round 2 and is now proven under load.
- **Real provider smoke**: DONE offline via two real Ollama models (qwen2.5:0.5b, smollm2:360m) through litellm's `ollama/` provider — genuine calls, genuine token counts, full ledger assertions (tests `TestOllamaSmoke`). Cloud tiers (Anthropic/OpenAI) are written and self-skip: this machine's `ANTHROPIC_API_KEY` holds a Groq-format placeholder and `ANTHROPIC_BASE_URL` points at local Ollama; `OPENAI_API_KEY` unset. The smoke tests auto-activate for anyone with valid cloud keys.
- **CLI (Boundary 6)**: Terminal 4's `cli/` has landed and already
  resolves `runtime.scheduler.run` via `cli/deps.py` (verified: their
  `run-benchmark` fans out through the real scheduler). No runtime-side
  changes were needed; the `--approval`-style supervision interactions
  are covered by the worker/scheduler contract.

## Test coverage (all green as of Round 5)

`tests/test_model_router.py` (21), `tests/test_difficulty_approval.py` (8), `tests/test_scheduler_integration.py` (14 — real process kills, hang detection, approval file protocol, ablation toggle, **gate-park/hang interaction**), `tests/test_provider_smoke.py` (Ollama 2 passed; cloud 3 self-skip). Full-suite run at Round-5 closeout (all terminals' tests, post both closeout fixes): **300 passed, 3 skipped, 0 failed**; runtime-module-only run: 48 passed / 3 self-skip. Stress runs (`python -m runtime.stress`) are separate from pytest by design.

Definition-of-done checks, verified not assumed:
- ✅ Scheduler runs 10+ concurrent fake tasks; concurrency cap proven from event journal (`max_overlap <= cap`); parallel beats serial floor.
- ✅ Mid-task crash (hard `os._exit`) → resume from completed steps, proven by: 2 worker starts (fresh, resume), state.json all-steps-complete, attempt=2, event journal (`crash`→`crash_retry`→`finish`).
- ✅ Hang → killed via stale state.json + one-shot injection disarm → completes after resume.
- ✅ Adaptive routing toggle demonstrably changes model choice + ledger cost (ablation architecture test).

## Known limitations / decisions future-you should know

1. **Windows process semantics**: `proc.kill()` on Windows is hard-kill ( TerminateProcess) — workers get no cleanup chance. That's exactly the crash scenario we checkpoint for, so it's fine (and the fake's `os._exit` matches).
2. **Hang detection granularity**: state.json mtime staleness requires the harness to touch state.json per step; if the real harness goes minutes between state writes (e.g. one long test run), raise `hang_heartbeat_stale_s` accordingly or add a step-level progress file. Measured (r4-real-45-45-8): healthy work-phase gaps up to ~98s (p95 75s) under 45-way Docker load — size the window above your real work, not just above your model calls. **Gate-parked workers are exempt from the state-stale kill while their checkpoint says `awaiting_approval` (or `finished`) and the heartbeat is fresh** — the approval park is not a hang; heartbeat death and the wall-clock cap still kill.
3. **PID reuse** (fsutil `is_pid_alive`): heuristic only; never used for correctness decisions.
4. **Approval gate file protocol** assumes one approver; concurrent conflicting decisions resolve last-write-wins via atomic replace.
5. **The ablation IS run** (this round): v1 honest negative + v2 positive result archived under `logs/ablations/` with proxy-price honesty notes. Next step for Phase 6: bigger task set, repetitions, real paid tiers.
6. **`logs/` is gitignored** — all run state (ablations, stress) lives there; nothing in-repo depends on committed artifacts. Summary stats are recorded HERE and in each run's `summary.json`/`stress_report.json`.
7. **Cheap-tier endpoint variance**: nararouter (qwen3.8-27b) went from 12/12 instant replies to 90-240s/call and one hard-down window within a single evening; the router's transient retry is what saved the v2 ON arm. If a future ablation behaves erratically, check endpoint health first (the probe scripts pattern is in the Round-2 story above).
