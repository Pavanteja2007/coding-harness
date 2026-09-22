# runtime/ — Terminal 3: Concurrency, Reliability, Model Routing

## Cross-Task Learning round (2026-09-14) — offline history analysis + predictor recalibration loop

An MLOps-style continuous-improvement loop over the accumulated run
history — not per-task retry. New module `runtime/analyze_history.py`
(+ a small opt-in hook in `difficulty.py`); CLI surface
`vex analyze-history`. All data sources are the EXISTING documented
formats (trace.jsonl, model_ledger.jsonl, ablation summary.json) — the
job is a pure consumer; no producer changed.

### Task A — the aggregation job (`runtime/analyze_history.py`)

`scan_tasks(logs_root)` walks the whole logs/ tree and builds one
record per REAL fix task (issue text, config, outcome, attempts,
repair fires, retrieval strategy, ledger rollup, re-derived task-level
difficulty prediction via the same
`ensemble.predict_task_difficulty` shape the router ingress scores).
Exclusion filters, each pinned by a test, matter because the first
naive scan produced 352 "tasks" of which a third were garbage:

- **Scripted/fake runs excluded**: `logs/evals/**` (every eval arm is a
  scripted model by design), stress/soak/abuse/memplan-pilot/dod/
  sandbox-* dirs, and any task whose task_start config carries
  `use_mock_provider` / `mock_script` / `use_fake_harness`. Their
  outcomes measure the SCRIPT, not routing.
- **Archived task dirs excluded**: the harness's `_fresh_paths`
  renames prior runs to `{task_id}.old-<ts>/` (and build_mode stages
  `{task_id}.base/`) — the live sibling's trace is CUMULATIVE (the
  resume contract) so archives are stale prefixes; scanning them
  double-counted 77 task_ids on the real tree before this filter.
- **Non-fix modes excluded**: question/research (mode rides a `mode`
  event + task_end field, not config) and build (config.mode) — no
  fix-loop difficulty semantics.
- **OFF-arm/pinned tasks marked `routed=False`**: routed_via_hint is
  None on every pinned ledger row; their outcomes measure the
  expensive ENDPOINT, not the predictor (see below).

The three aggregates over the records:
1. **Predictor divergence** (routed tasks only): aligned /
   false_escalation (hard-predicted, solved clean on cheap) /
   missed_escalation (easy/medium-predicted, struggled or failed) /
   hard_aligned + counts of pinned tasks explicitly NOT scored.
2. **Retrieval strategy vs repairs**: structural+grep vs grep-only vs
   other/none — mean attempts, mean repair fires, mean cost, status
   mix. Honest caveat in the report: OBSERVATIONAL (strategy
   correlates with repo shape; error-died tasks skew the low rows).
3. **Failure patterns**: bucketed task_end status×reason (verifier-
   refused failed / endpoint-model errors / timeouts / budget) with
   per-bucket task ids + reason strings.

### Task B — the offline recalibration + the REAL before/after

`calibration_rows` (strict label policy) → deterministic GROUPED
split (by bug — the same bug re-run across ablation windows is ONE
observation; grouped so a bug never straddles train/holdout) →
`recalibrate` (grid search over the same 2-parameter band family as
`score_to_hint`'s (easy_max, hard_min) — recalibration, NOT
re-architecture; no new features) → `evaluate` before/after on the
held-out bugs.

Label policy, the part that took honest thinking (two semantics,
both in the report on purpose):
- **Divergence aggregate** describes the REPAIR PROCESS: any second
  attempt or verifier failure counts as "the predictor could have
  warned us".
- **Calibration labels** target ROUTING ECONOMICS: only
  `status="failed"` (verifier refused after the full attempt budget on
  the cheap tier) is "hard" — a cheap attempt-2 retry is a documented
  economic WASH vs an expensive planner call (the IR2 ensemble
  ablation measured 2x-cheap-candidates ≈ 1x-expensive-call at these
  tiers), so "needed a retry" is NOT evidence escalation would have
  helped. error/timeout tasks are EXCLUDED from labels entirely
  (endpoint deaths are endpoint evidence — the v1 rate-limit-artifact
  lesson applied in reverse; labeling a hung planner call "hard bug"
  would teach the predictor to escalate on latency).

**The real result over this machine's accumulated history
(logs/analyze-history/20260914-133838/report.json, the honest
headline): the recalibration did NOT beat the v2 predictor — the
improvement is MARGINAL and nothing was applied.**

| view (per-bug) | acc | missed esc | false esc |
|---|---|---|---|
| held-out, v2 bands (before) | 0.75 | 1 | 0 |
| held-out, refit bands (after) | 0.75 | 1 | 0 |
| train, v2 bands | 0.7222 | 3 | 2 |
| train, refit bands | 0.7778 | 3 | 1 |

Dataset reality behind that: 294 real tasks scanned, 109
adaptively-routed, and after the strict label policy only ~22 bugs
carry usable labels (18 train / 4 holdout) — the refit bands
(easy≤0, hard≥5) would nearly eliminate hard predictions, and the
held-out bugs (all easy-labeled except slugify-case) can't validate
such a shift; per-run divergence shows the 2 real false-escalation
bugs (backoff-race, parse-comma — the known scary-text probes) and
the 3 real verifier-refused bugs (bug04-nameerror, boltons/inflect
ordinal teens, ens-a-slugify-case) score 0-3 on the intrinsic
features — **the current feature set does not separate easy from
hard on this data**, which is exactly why a threshold refit can't
buy anything here. The recommendation field said
`marginal - not worth applying (held-out delta zero)` and the
calibration file was NOT written (verified: no
runtime/difficulty_calibration.json on disk).

What WOULD make this loop pay: (a) more genuinely-hard real tasks
(SWE-bench Phase 6 — the current history is easy-dominated, which is
itself the honest characterization of the fixture/multirepo sets);
(b) richer intrinsic features than issue-text keywords (repo size/
test-suite shape/bug-class signals) — a THRESHOLD refit of the
current features is provably near-inert on this data, and that is a
finding, not a failure of the loop.

### The apply mechanism (built, gated, unused for now)

- `difficulty.py::score_to_hint` now reads an OPT-IN
  `runtime/difficulty_calibration.json` ({"easy_max", "hard_min"} +
  provenance) — absent/malformed/out-of-range file degrades to the
  built-in v2 bands (which are byte-identical behavior to before;
  pinned by the existing 8 difficulty tests re-run green). mtime-
  cached, never raises.
- `vex analyze-history --apply` writes the file ONLY when the
  report's held-out before/after actually improved
  (`recommendation == "apply"`); marginal/reject/insufficient →
  nothing written, message says so. Deleting the file reverts to
  built-in bands. Applying is a deliberate human-reviewed step —
  the loop is offline by design (never live/online).

### Task C — the documented maintenance job

`vex analyze-history [--log-root DIR] [--holdout-frac 0.25] [--json]
[--apply]` (also `python -m runtime.analyze_history`). WHEN to run it:
after any batch of real runs accumulates (post-ablation, post
milestone), and before quoting predictor/routing numbers — the same
discipline as the eval harness for prompts. Output:
`logs/analyze-history/<ts>/report.json` (the report carries its own
honesty notes; exit 0 on success, 2 on missing logs root). README's
routing section now documents the loop.

### Verification at close

- `tests/test_analyze_history.py` — **38/38** offline, deterministic
  synthetic log trees (no Docker/network): the four exclusion filters,
  archive double-count regression, divergence classes, retrieval
  grouping, both label policies (incl. the wash-rule + endpoint-death
  exclusions), grouped/deterministic split, per-bug evaluate, the
  fitter (perfect separation / insufficient data / band family),
  the apply gate (marginal→nothing, apply→file+provenance, bad
  shapes), the difficulty override (file wins, malformed falls back,
  out-of-range rejected, roundtrip), end-to-end build_report, and the
  CLI command (exit 0, --json, missing root→2, --apply noop).
- Runtime module suites re-run green: scheduler 14 + router 21 +
  difficulty/approval 8 + ensemble 11 + provider smoke → 57 passed,
  3 self-skipped.
- difficulty.py behavior with NO calibration file is byte-identical
  (active_bands() == (1,4); all 8 pre-existing tests pass unmodified).
- CLI suites: at round close, 3 test files were failing from a
  PARALLEL session's in-flight `harness/core.py` breakage
  (`run_step() got an unexpected keyword argument 'steer'`). That
  flag is now RESOLVED: re-verified afterwards, the parallel
  session finished the steer wiring — `tests/test_cli_vex2.py` +
  `tests/test_cli_errors.py` 37 passed, `tests/test_e2e_run_task.py`
  28 passed, everything green with this round's changes in the tree.
  (Re-verified test counts at this later check: analyze_history suite
  38/38; with difficulty/approval + router + ensemble: 81 passed;
  scheduler 14 passed; ruff clean.)
- ruff: analyze_history.py + test file clean; difficulty.py clean
  (one pre-existing RUF100 stale noqa removed in passing).
- runtime-owned changes: `analyze_history.py` (new),
  `difficulty.py` (+ the opt-in calibration-file read in
  score_to_hint; built-in bands untouched), `tests/
  test_analyze_history.py` (new), `cli/main.py` (+ the
  analyze-history subcommand — shared file, additive region only),
  README.md (+ the maintenance-loop section). No INTERFACES.md
  contract changes (all new surfaces are runtime-internal tooling +
  one CLI subcommand; Boundary 2's signature and semantics
  untouched).

## Improvement Round 2 (2026-09-10) — Multi-candidate ensemble routing (three-arm ablation)

### Task A — the mechanism (`runtime/ensemble.py`, new; the original adaptive routing is UNTOUCHED)

Extension of the novel mechanism: instead of escalating straight to the
expensive tier when the difficulty predictor flags a task as hard, try
generating TWO cheap-model candidate fixes in parallel first and escalate
only if BOTH miss. Architecture constraint that shaped the design: a
"candidate fix" cannot be a single call_model — verification lives in the
harness ABOVE Boundary 2 — so the ensemble is a TASK-LEVEL driver
composing the existing scheduler/worker/harness machinery;
model_router.py and difficulty.py are byte-for-byte unchanged (this is a
genuine extension of the mechanism, not a replacement — the single-
attempt ON arm remains exactly as ablated in v1-v6).

How it runs (two phases over the same scheduler):
1. Task-level difficulty predicted ONCE per bug from the issue text via
   `predict_task_difficulty` — same v2 predictor, same planner-message
   shape ("## Issue ... ## Retrieved context") the per-call router
   ingress scores, so the task-level hint EQUALS what the router
   decides on the planner call (test-pinned equality).
2. easy/medium → exactly ONE sub-task with the ON arm's config
   (adaptive routing on, tiers cheap/cheap/expensive — struggle
   escalation still available mid-task). This isolates any
   ensemble-vs-ON delta to the hard-predicted tasks.
3. hard → TWO sub-tasks (ens-c1-/ens-c2-<slug>) pinned to the CHEAP
   tier (adaptive_routing False + provider/model/key/base = cheap
   entry — a candidate is a full verifier-gated run on one model),
   run in parallel by phase-1's single scheduler invocation.
4. Phase 2 (only if a hard bug's candidates ALL missed — any
   non-success counts, so a dead cheap endpoint escalates): ONE
   escalation run (ens-x-<slug>) pinned to EXPENSIVE, OFF-arm
   semantics (full attempt budget). A winning candidate skips
   escalation entirely.

Honest properties, encoded in the runner's honesty notes: both
candidates ALWAYS run to completion and BOTH costs count (parallel
generation is a latency/resilience trade at real cost); the escalation
run is a FRESH attempt — it does not see the failed candidates'
transcripts (no cross-run context channel exists; documented, not
hidden); when both candidates verify, c1 is reported as the winner
(deterministic; both recorded in sub_tasks). Per-repo passthroughs
(target_test/test_command) ride the same keys the multirepo set uses.
Offline test coverage: tests/test_ensemble.py (11) — strategy
selection, candidate pinning (task.json config verified from disk),
double-miss escalation, any-winner skip, aggregation/stats shapes,
and the ingress-equality guarantee — through REAL scheduler worker
subprocesses with the fake harness + a monkeypatched-scheduler unit
test for the mixed-outcome path. Zero edits to model_router.py /
difficulty.py this session — single-attempt adaptive routing keeps
its exact v1-v6-ablated behavior (this tree's diff on those files is
prior rounds' documented in-flight work).

Wiring: `python -m runtime.ablation --arm ensemble` (choices grew;
separate invocation merges into the shared summary.json like any arm —
the Round-4 accumulate behavior). Ensemble stats reuse the
ablation.collect shape (+ strategy/candidate_win/escalated fields) so
summary consumers stay uniform.

### Task B — the three-arm ablation (`logs/ablations/ir2-final/`, all 16 tasks, real runs, same evening window, every number re-derived from the on-disk ledgers by probe_logs/verify_ir2.py)

| arm | success | calls | tokens | cost | wall | vs OFF cost |
|---|---|---|---|---|---|---|
| OFF (always-expensive) | 11/16 69% | 102 | 284,193 | $0.3773 | 3624s | 1.00x |
| ON (single-attempt adaptive) | 15/16 94% | 122 | 303,227 | $0.1119 | 1848s | **0.297x (3.37x cheaper)** |
| ENSEMBLE (multi-candidate) | 15/16 94% | 126 | 280,732 | $0.0947 | 1303s | **0.251x (3.98x cheaper)** |

- Headline: ensemble cheapest overall (-15% vs ON, -75% vs OFF) at
  equal 94% success — but the honest per-task breakdown says the
  arm-level win is NOT the ensemble mechanism's doing. The two
  hard-predicted tasks (parse-comma, backoff-race — same two the v4
  run escalated on the planner call; task-level predictor matches the
  router's per-call decision, test-pinned) are where the strategies
  differ, and THERE the ensemble was cost-neutral-to-slightly-worse:
  parse-comma ON $0.0104 vs ENS $0.0105 (wash), backoff-race ON
  $0.0063 vs ENS $0.0072 (+15%) — two full cheap candidate runs cost
  about the same as one expensive planner call + cheap completion at
  these tiers ($0.20/$0.60 vs $0.60/$2.20 per 1M tok). Both
  candidates SUCCEEDED on both hard tasks (candwin=2/2, zero
  escalations ever fired), so the "look hard but aren't" saving is
  real but fully consumed by the second candidate's overhead.
- The arm-level -$0.0172 delta is dominated by the 14 identical-config
  tasks' run-to-run variance (incl. ON's stochastic mid-run struggle
  escalation on path-slash — ON hint=easy both arms, yet ON's call #7
  went hard for $0.0025 while the ensemble's own path-slash run stayed
  all-cheap: per-call struggle is endpoint/model-load dependent, not
  the ensemble's doing).
- Success spread is capability noise, not routing: ON failed bug04
  (attempt 2 exhausted turns), ensemble failed slugify-case (fix
  APPLIED + pytest green, then the agent-tests phase exhausted turns
  without SUBMIT) — each task passed under the other arm's identical
  routing. OFF's 5 non-successes are the v6 pattern again: expensive-
  tier latency deaths (3 wallclock timeouts, 2 errors, 6-15 calls
  burned each) in a window where glm-5.3-free ran p95 ~300s+.
- Wall: ensemble fastest (1303s) — the two hard tasks' candidates ran
  in parallel within phase 1. Zero expensive-tier calls in the
  ensemble arm (126/126 stepfun).

**Verdict (honest, and a legitimate reportable outcome): the ensemble
did NOT clearly beat the existing single-attempt adaptive mechanism on
this task set.** At the mechanism's actual operating point (hard-
predicted tasks), 2x cheap-candidate cost ≈ 1x expensive-planner-call
cost — a wash — and the arm-level 15% win is variance-dominated. The
ensemble's real properties, demonstrated: success resilience on hard
tasks held (2/2 candidate wins, escalation path never needed), zero
expensive-tier dependency on hard-predicted tasks, and a structural
insurance property this set couldn't price (the double-miss→escalation
path never fired: if cheap candidates genuinely can't solve a hard
task, the ensemble pays 2x-cheap overhead and still gets the expensive
attempt, vs ON's mid-task struggle escalation which fires only AFTER
burned turns). When WOULD it win: with a much wider cheap:expensive
price ratio (at 3x, two full cheap runs ≈ one expensive call; at 10x+
the pair would cost less than a fifth of the escalation it replaces),
or when candidate DIVERSITY rescues tasks a single cheap attempt
fails — this set's 2 hard tasks were both candidate-winners, so the
rescue effect remains unmeasured (0 escalations = no data on the
double-miss path in real conditions; only the offline tests cover it).
Phase-6 SWE-bench runs on genuinely-hard tasks are where this mode
should be re-tried; on this easy-dominated set it's a defensible
alternative, not an upgrade.

Method notes: arms ran as sequential separate invocations sharing
--out (per-arm crash resilience; the merge behavior accumulated all
three + arm_runs provenance) via probe_logs/run_ir2_ablation.py,
detached — the first attempt to run OFF+ON in one invocation hit BOTH
a runner-usage error (repeat --arm doesn't accumulate; argparse takes
the last — only ON launched) and the 60-min shell cap killing it
mid-run; that partial run was deleted, NOT quoted. Standing caveats
unchanged (proxy prices, n=16 x 1 rep, endpoint variance, arms
sequential in one evening). Endpoint health probed first
(probe_logs/endpoint-ir2.json: both tiers alive, 24s/20s trivial
call — slow window, which is what killed OFF's three timeouts).

### Improvement Round 2 status

- Task A: ensemble mechanism built + wired + offline-tested (11/11);
  original routing mechanism untouched by this round (zero edits to
  model_router.py / difficulty.py this session — the working tree's
  diff on those files is prior rounds' documented in-flight work:
  Round-6 price rows, Round-8 tracing + max_completion_tokens).
- Task B: three-arm run complete, all numbers disk-verified; honest
  verdict documented above (no clear win; conditions under which it
  would win recorded).
- Module tests re-run green: 59 passed, 3 self-skipped (scheduler 14 +
  router 21 + difficulty/approval 8 + ensemble 11 + provider smoke
  2 active/3 self-skip... cloud-key smokes: 5 active/3 self-skip
  counted per prior convention). Ruff: new files clean (ensemble.py,
  test_ensemble.py formatted + 0 violations; ablation.py within its
  baseline count).
- runtime-owned changes: `ensemble.py` (new), `ablation.py` (+arm
  ensemble + three-arm delta block + honesty note), `tests/
  test_ensemble.py` (new), probe_logs scratch (endpoint probe,
  driver, disk-verification script — gitignored evidence). No
  INTERFACES.md contract changes (all new surfaces are runtime-
  internal tooling; Boundary 2's signature and semantics untouched).

## Round 7 (2026-09-09) — production readiness: CI, soak, consolidated write-up

### Task A — CI pipeline (`.github/workflows/ci.yml`, runtime-owned)

Two jobs, split by cost (the stress/abuse suites take minutes BY DESIGN —
that IS the measurement — so per-push CI gets a fast subset):

- **`tests` (every push/PR, ubuntu-latest, ~5 min)**: the runtime
  pytest modules (scheduler 14 + router 21 + difficulty/approval 8 +
  provider smoke 5 active/3 self-skip — cloud smokes self-skip without
  keys, so CI stays green without secrets) + a LIGHT stress scenario:
  `python -m runtime.stress --tasks 20 --concurrency 20 --kill 4 --mode
  fake` — the full machinery (real scheduler, real worker processes,
  mid-run kills, resume proof, cap proof) at a size that runs in ~5s.
  Verified locally before landing: 10/10 checks pass in 5.1s
  (logs/stress/ci-light-smoke).
- **`stress` (nightly 03:30 UTC + workflow_dispatch, ~60-90 min)**:
  full pytest suite + Docker-warmed full-scale stress (fake 50@50/12,
  fake 50@30/20, real 45@45/8, real+approval 45@30/8 — the four
  Round-5 closeout scenarios) + the abuse suite (6/6). Real-mode and
  abuse steps are `runner.os == 'Linux'`-guarded (real-mode scripts
  are POSIX sed; abuse needs the Docker sandbox) — fake-mode stress is
  fully cross-platform. Reports uploaded as artifacts (14-day
  retention).

Honest limitation (documented in the workflow): real-mode stress in CI
is unproven on the GitHub runner until the first nightly actually runs
(scenario code itself is the locally-green Round-5 methodology; the
Linux-side `sed -i` scripts were authored for this Windows box's Docker
VM and are expected to port, but the first nightly is the proof).

### Task B — long-duration soak (`runtime/soak.py`, new; NOT in pytest)

One long-lived Scheduler across 120 sequential batches x 30 tasks
(3,600 tasks total, 5.15 simulated task-hours of aggregate task
execution, ~13.5 min wall), cap 30, 3 kills every 3rd batch (120 kills),
fake-harness tasks through REAL worker processes (same trade as
stress.py fake mode: the supervision machinery under test —
spawn/reap/kill/requeue/resume/journals/checkpoints — is fully real).
15 checks, all asserted from measured data: all-success, per-kill resume
proof, cap proof per batch, scheduler RSS growth, latency p95 drift
(last quarter vs first), per-task artifact bounds + flatness,
checkpoint/state/heartbeat counts, attempt-dir bounds (journal-derived),
journal growth flatness, zero leaked children at batch boundaries, .tmp
residue. Profiles: default / quick / ci (+ full per-knob CLI overrides).
**Final run: 15/15 PASS** (logs/soak/r7-final/soak_report.json, wall
805s): latency ratio 1.02x, files/task flat 7.06→7.07, zero natural
crashes, 120/120 kills resumed, zero .tmp residue.

**The soak found a REAL bug (this is why soaks exist):** 5 of 3,600
workers (first full run) died with `PermissionError(13)` inside their
atomic state/checkpoint writes — on Windows, a concurrent READER
(no FILE_SHARE_DELETE in the CRT open) makes os.replace transiently
fail. The crash-resume machinery absorbed all 5 (3600/3600 success —
the resilience layer held), but the same race exists in real mode:
the scheduler's hang check content-reads heartbeat.json/checkpoint.json
while workers replace them every 2s. FIX (runtime-owned,
`runtime/fsutil.py`): `atomic_write_json` now retries the final
replace on PermissionError (3 attempts, 50ms apart — a sharing
violation clears when the short-lived reader closes), and
`fake_harness._write_state` routes through the shared primitive
instead of hand-rolling the same replace. Re-run: 0 occurrences
(was 5/3600; the r7-final run shows 121 crash events for 120 kills
→ 0 natural, vs 125/120 before the fix).

**RSS growth honestly attributed:** +35-40MB over the run (19.7→57MB),
passes the ≤50MB decile check, but does NOT plateau inside the window
— a tracemalloc probe (40-batch run, 10-frame stacks) shows only
**1.1MB of Python objects retained at end** (top site: one pathlib
stat cache entry), so the growth is C-level allocator/arena churn from
spawning/reaping 3,600 subprocesses, not a scheduler object leak
(scheduler dicts stay task-count-sized: spawns/crash-budget/active).
Documented as the interpretation; the check bounds the observable
(RSS), the probe bounds the cause (traced retention).

**Two soak-harness lessons encoded in the module:** (a) the killer's
select→fire window can race a fast-finishing victim (5s tasks vs
stress.py's minutes) — firing now re-checks liveness and drops
finished victims instead of reporting kills that never landed;
(b) every invocation needs its OWN --out dir (fixed run_id "soak"
inside it) — the tracemalloc probe's second invocation contaminated
the first's report through the shared journal (diagnosed from
duplicate task spawns in one journal; the probe's "resume=false"
anomalies were that contamination, NOT a runtime bug — both clean
runs show 120/120 correct resumes).

### Task C — consolidated results write-up: **`RESULTS.md`** (repo root)

One clean, final, resume/interview-grade document consolidating
v1→v4 + v6-multirepo + the abuse/soak robustness evidence: the
one-paragraph summary, method, full results table, the honest v1
negative kept in full, per-run interpretation (bounded false
escalation, genuine escalations), out-of-distribution confirmation
(+20% success at 24% cost, with the wall-clock-budget interaction
finding), robustness results, and the standing honest caveats (proxy
pricing, n, task shape, endpoint variance). Every number re-verified
against the on-disk summary.json/ledgers this round (v4 delta:
cost_ratio 2.589, both arms 1.0 success; v6 delta: 4.192, -0.2 success
delta ON-favorable).

### Round 7 status

- Task A: CI workflow landed + YAML-validated; light stress verified
  locally (5.1s, 10/10 checks). First nightly run pending (will prove
  the Linux real-mode port; see honest limitation above).
- Task B: soak harness built; 3 full default runs + 1 quick + 1
  tracemalloc probe; final run 15/15 PASS; 1 real bug found+fixed
  (fsutil PermissionError retry), RSS honestly attributed (allocator
  churn, not a leak — 1.1MB traced retention).
- Task C: RESULTS.md written (repo root), numbers disk-verified.
- runtime-owned changes: `soak.py` (new), `fsutil.py`
  (PermissionError replace-retry in atomic_write_json),
  `fake_harness.py` (_write_state through atomic_write_json),
  `.github/workflows/ci.yml` (new). No INTERFACES.md contract changes
  (atomic_write_json's signature/semantics unchanged — only a bounded
  internal retry on a transient Windows error mode; the retry cannot
  mask a real single-writer violation because a persistent denial
  still raises after 150ms).

## Round 6 (2026-09-09) — multi-repo ablation + adversarial cost-abuse hardening

### Task A — the ablation extended to REAL unfamiliar OSS repos (v6-multirepo)

**Decision documented (T1's Round-6 multi-repo tasks had not landed when
this round started — verified: no Round-6 entries anywhere in the tree;
per the AGENTS.md convention I built the set myself rather than skipping
or stalling).** Terminal 1's Round-4 OSS pattern (jaraco/path: clone a
real repo at a pinned SHA, introduce one GENUINE bug, encode it in a
failing regression test), scaled to a five-repo task set:

| repo | pin | bug class (introduced, genuine) |
|---|---|---|
| more-itertools | ca711220a6 | `recipes.nth` wrong-index (islice off-by-one) |
| arrow | 2224255c4a | `util.next_weekday` weekday-mapping (+1 shift) — upstream's own test_next_weekday catches it |
| inflect | 262a247d2d | `ordinal()` teen-table ignored (111→"111st") |
| semver | 6adf8765f6 (v3.0.4) | `next_version("prerelease")` drops custom token |
| boltons | 961dcff3f4 | `strutils.ordinalize` teen condition on wrong digit |

New module `runtime/multirepo_tasks.py`: pinned-SHA clones cached under
`logs/multirepo-cache/` (gitignored run state), bug+test baked per run
under the run's own out dir, per-repo suite pins (see quirks below),
`--check` host self-verification — 5/5: each buggy tree FAILS its
regression target, canonical fix makes target+pinned suite green
(Docker-verified through `execution.verify` too), issue text predicts
easy (score 1, no hard-saturation). `runtime/ablation.py` grew
`--tasks multirepo` (per-bug target_test/test_command overrides; the
bug_sources + honesty notes go into summary.json).

**Per-repo quirks found and pinned (all live-diagnosed):**
- semver: git SYMLINKS in tests/ materialize as path-text files on
  Windows → fixed at bake (copy target over link, T2's documented
  clone-time hazard); `.pytest.ini` addopts need pytest-cov/doctests →
  `-o addopts=` (pythonpath=src survives as a separate key).
- arrow: tox.ini `[pytest]` addopts need pytest-cov → `-o addopts=`;
  test files needing pytest-mock/pytz/simplejson are dev extras the
  deps image does install BUT the suite pin stays `test_regression +
  test_util.py` (blast radius of the bug).
- inflect: deps image installs `[project.optional-dependencies]` groups
  (incl. the `check` extra → pytest-ruff), whose pseudo-tests fail on
  the Docker bind mount's executable bit (EXE002) → `-p no:ruff`
  pinned. Cross-repo dep: inflect imports more_itertools — the image
  installs it (it's in [project] dependencies); the HOST check adds
  the pinned more-itertools clone to PYTHONPATH for parity.
- more-itertools: upstream's own `PrimeFunctionTests` grinds MINUTES on
  30+-digit pseudoprimes (found live — the first `--check` "hang" was
  this, not a bug) → deselected in the suite pin.

**Cheap-tier endpoint died mid-project (the documented hazard, live):**
qwen3.8-27b @ router.bynara.id returned "Insufficient credits" on every
call (free-tier credit exhaustion). Probed replacements live:
longcat-2.0-free was REJECTED after a real smoke run (replies with
pseudo-XML `<longcat_tool_call>` markup the bash-only harness can't
execute — burned a whole 6-turn session on bash syntax errors;
logs/ablations/mr6-smoke); nemotron-3.5-lightning-free gateway-flakes
on long prompts; **stepfun-3.7-flash is the new cheap tier** (clean
commands, fenced replies the harness strips by design). Expensive
tier unchanged (z-ai/glm-5.3-free @ tokenrouter, alive). Proxy price
table updated in both places (ablation.py + model_router._PRICES).

**v6-multirepo result (both arms, real runs, `logs/ablations/v6-multirepo/`,
concurrency 5, max_wallclock 1500s):**

| arm | success | calls | tokens | cost | wall |
|---|---|---|---|---|---|
| OFF (always-expensive) | 2/5 40% | 71 | 329,438 | $0.3059 | 2992s |
| ON (adaptive) | 3/5 60% | 75 | 302,801 | $0.0730 | 581s |

- **ON beat OFF on BOTH axes: +20% success at 24% of the cost
  (4.19x cheaper), 5.1x faster wall.** The cost-savings direction HOLDS
  on genuinely unfamiliar repos — it was NOT an artifact of the
  fixture/synthesized set. Escalations 0: the ON arm ran 100% cheap-tier
  (75/75 stepfun calls; every issue predicted easy — these are
  one-function bugs with natural short texts, exactly what the v2
  predictor is calibrated for).
- **Honest headline shift: absolute success DROPPED vs the old set
  (100% → 40/60%).** The failures are genuine CAPABILITY failures, not
  machinery failures — all three failed agents LOCATED their bug
  (last commands show them reading the exact buggy lines) but never
  applied the edit before turns/retries ran out (`files_touched: []`
  on every failure). Unfamiliar-repo difficulty is real and the
  multi-repo set is harder than it looks from the one-liner diffs.
- The OFF arm's 3 non-successes are endpoint-latency deaths, not
  harness bugs: glm-5.3-free calls measured p95=309s, max=976s, and the
  semver task's PLANNER call hung past the 1200s stale window twice
  (hang-kill → requeue → exhausted → timeout with 0 ledger calls; the
  request never returned). The ON arm's cheap tier ran p50=22s /
  p95=61s / max=98s — which is WHY ON finished more tasks: more
  attempts fit inside the same wall-clock budget. That interaction
  (routing tier affects not just cost but how many attempts fit the
  wall budget) is a real finding the old task set could never show.
- n=5 x 1 rep, free-tier endpoints, proxy prices — directional, per
  the standing honesty notes. The v6 summary.json carries the full
  per-task ledgers.

**Bottom line for the ablation claim:** the mechanism's cost result
reproduces OUT of distribution (4.19x here vs 2.59-3.47x on the old
set), and multi-repo evidence suggests adaptive routing may even HELP
success at scale (cheap tier's speed → more attempts per wall budget),
but the agents' absolute fix rate on unfamiliar repos needs the
Phase-6 step up (SWE-bench, paid tiers) before any resume-grade
success-rate claim.

### Task B — cost/resource-abuse hardening (adversarial, all caps FIRED)

New standalone harness `runtime/abuse.py` (stress.py conventions:
real scheduler workers, real Docker sandbox/verify where a bash loop
runs, hostile/deterministic model behavior via the worker-process mock
paths; NOT in the pytest suite — scenarios deliberately take minutes
to hit their caps; that IS the measurement). Six scenarios, each
deliberately triggering the worst case, verdicts measured from
ledgers/journals/walls and asserted; final run ALL PASS
(`logs/abuse/final-r6/abuse_report.json`):

1. **retryloop** — a REAL localhost endpoint returning 429 forever.
   Router's bounded backoff (rate_limit_retries=2, base 2s) exhausted
   in ~23s, task ended `error` (planner call failed after 2 retries +
   1 transient retry), never unbounded sleeping. Cap proven under
   adversarial congestion.
2. **overshoot** — budget_cap_usd=$0.10, every reply a giant priced
   completion. MEASURED: the check fires at attempt START, so one
   full in-attempt session (6 turns) can burn past the cap before the
   next check — final $0.93 = **9.28x cap, one-attempt-bounded, never
   unbounded** (status `failed` at attempt 2's budget check). This is
   the honest granularity limit of budget_cap_usd: cap + one attempt's
   worth of calls. Documented, not hidden.
3. **escalate** — issue text engineered to maximize the difficulty
   score (stack traces, race/deadlock/flaky/intermittent wording) +
   real failing-test output burned into the tail. Under adaptive
   routing: 1 hard call / 6 total — the predictor escalated ONE call
   (the planner, on the scary text) then dropped back to cheap while
   the struggle signal accumulated. **Bounded false escalation
   confirmed adversarially** (16.7% hard, never all-hard).
4. **runaway** — `sleep 9900` step commands. Layered kills held:
   sandbox `command_timeout_s=20` killed the container (twice), the
   run finished `failed` in 54.7s vs the 120s wall cap, zero container
   residue.
5. **crashloop** — both halves of the anti-crash-loop chain: zero
   budget → 1 spawn → `crash_exhausted` → terminal `error` (never
   lost); crash_retries=2 → crash → requeue → relaunch DISARMS the
   fault injection (one-shot by design, worker.py) → resume success.
   A perpetual same-crash respawn is impossible BY CONSTRUCTION —
   the disarm is the loop breaker, now proven at both ends.
6. **prestate** — a REAL hung model call (localhost TCP endpoint that
   accepts and never responds) BEFORE any step completes. The
   state-stale hang check fired at exactly hang_heartbeat_stale_s
   (30.1s) — the harness writes state.json at task start, so even a
   pre-plan hang is hang-detectable (better than my design assumed);
   kill_exhausted → terminal `timeout`. (The wall-clock cap remains
   the backstop; this run never needed it.)

**Two suite-harness bugs found and fixed while building it (the suite
eating its own dogfood):** (a) `run_id == task_id` made the scheduler's
run dir collide with the harness's logs/{task_id}/ dir → Windows
PermissionError on _fresh_paths archive — run ids now `run-abuse-*`;
(b) `.runtime` model ledgers PERSIST across invocations, poisoning the
second overshoot run's cost measurement with the first run's $3.36 —
`_run_one` now wipes stale task dirs before each scenario (the ledger
append semantics are correct for RESUMES; per-invocation measurement
needs the wipe).

**Honest gaps found (not fixed this round, documented):** budget
granularity is attempt-level (finding 2) — a per-call pre-check in
ModelClient/routing would tighten the cap to +1 call, at the cost of a
router-side budget read; wall-clock and hang caps interlock correctly
but the hang check needs state.json to exist, and TaskState's early
write is what saves the pre-plan case (if a future harness change ever
delays that first write past hang_heartbeat_stale_s, only the
wall-clock cap bounds pre-plan hangs — keep the early write).

### Round 6 status

- Task A: multi-repo set built+self-checked (host AND Docker), ablation
  v6-multirepo RUN — cost result reproduced out-of-distribution
  (4.19x), success-direction favorable, absolute success honestly
  lower on unfamiliar repos (capability, not machinery).
- Task B: adversarial abuse suite built+green (6/6) — every cap proven
  to FIRE under deliberate worst-case triggering, two measured
  granularity limits documented (budget = cap+one-attempt; escalation
  bounded to ~1/6 under engineered scary text).
- Module test suite re-run post-changes: green (see Test coverage).
- runtime-owned changes: `multirepo_tasks.py` (new), `abuse.py` (new),
  `ablation.py` (--tasks multirepo + per-bug overrides + endpoint
  notes), `model_router.py` (+2 price-table rows). No INTERFACES.md
  contract changes (all new surfaces are runtime-internal tooling; the
  ablation's per-bug config keys are task.config passthroughs T1
  already supports).

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
| `difficulty.py` | **Real** | **v2**: intrinsic issue signal (first-user-message issue extraction, scaffolding-stripped) + struggle escalation from conversation tail; "llm" estimator w/ heuristic fallback unchanged. **Cross-Task round: score_to_hint reads an OPT-IN calibration file (runtime/difficulty_calibration.json, written only by the gated analyze-history --apply path); absent file = built-in v2 bands, byte-identical.** |
| `analyze_history.py` | **Real (Cross-Task round)** | The offline history-analysis + predictor-recalibration maintenance job: scan accumulated logs (scripted/archive/mode/OFF-arm filters), aggregate predictor divergence / retrieval-vs-repairs / failure patterns, refit score bands on a grouped per-bug train split, honest held-out before/after, gated apply. `vex analyze-history` / `python -m runtime.analyze_history`; report under logs/analyze-history/<ts>/. |
| `scheduler.py` | **Real** | Process-per-task supervision, concurrency cap (proven at 10-50), FIFO queue, wall-clock + hang timeouts, per-task crash budget with resume-on-relaunch, run-level event journal. **NEW: `live_attempts()` public view; spawn pins resume_dir/log_root (split-brain fix).** |
| `worker.py` | **Real** | `python -m runtime.worker --task-json <path> --run-dir <path>`: loads Task, sets router context + ledger, heartbeats, runs `run_task`, approval gate, writes `result.json` + checkpoint. |
| `approval.py` | **Real** | Cross-process file protocol: worker writes `request.json` and blocks; external approver writes `decision.json`; timeout → failed. Crash-restart-safe. |
| `checkpoint.py` | **Real** | Runtime-owned resume bookkeeping under `logs/{task_id}.runtime/`: `checkpoint.json` (atomic), `heartbeat.json`, `events.jsonl`. |
| `ablation.py` | **Real** | The Phase-5 ablation runner: task sets fixtures(5)/extra(11)/all(16)/**multirepo(5, Round 6)** × on/off/**ensemble(Improvement Round 2)** arms, real stack end-to-end, per-arm success/cost/token stats from ledgers, honesty notes baked into summary.json, three-arm delta block. Per-bug target_test/test_command overrides (the multirepo set needs them). Final runs: `logs/ablations/v3-expanded-fixed`, `v4`, `v6-multirepo`, `ir2-final` (three-arm; see write-ups above). |
| `ensemble.py` | **Real (Improvement Round 2)** | Task-level multi-candidate ensemble routing: predict difficulty once per bug (same v2 predictor + planner message shape the router ingress scores), easy/medium → ON-arm-identical single run, hard → 2 parallel cheap-pinned candidate runs + escalation to expensive ONLY on double miss. Two-phase scheduler composition; sub-task ledgers aggregated per bug. Offline tests (11) via fake harness through real worker subprocesses. The per-call router + predictor are untouched — additive, separately-ablated mode. |
| `ablation_tasks.py` | **Real** | The 11 synthesized Round-3 ablation repos (built at run time under the run's own out dir; `--check` self-verifies each fails pre-fix / passes post-fix on the host). Style-labeled issue texts incl. 2 scary false-escalation probes. **Round 4: `--check` hardened against a stale-`__pycache__` false verdict — several canonical fixes are byte-for-byte the same length as the bug (`upper()`→`lower()`, `order[1]`→`order[2]`), so a rewritten module could share (coarse-mtime, size) with its cached buggy bytecode and CPython would reuse the STALE pyc; check now runs with PYTHONDONTWRITEBYTECODE=1 + `-p no:cacheprovider` (11/11 across 3 consecutive runs after, was ~1 flaky BAD per 2-3 runs).** |
| `multirepo_tasks.py` | **Real (Round 6)** | The 5 REAL OSS repo tasks (more-itertools/arrow/inflect/semver 3.0.4/boltons at pinned SHAs, one introduced genuine bug each + failing regression test). Pinned-clone cache under `logs/multirepo-cache/` (env MULTIREPO_CACHE); bake applies bug+test per run; `--check` host-verifies fails-pre-fix/green-post-fix + sane difficulty prediction (5/5, host AND Docker-verified through execution.verify). Windows symlink + pytest-ini-quirk pins documented per task. |
| `abuse.py` | **Real (Round 6)** | Adversarial cost/resource-abuse suite (stress.py conventions; standalone, NOT in pytest): 6 worst-case scenarios — 429-forever endpoint (real localhost HTTP), budget-cap overshoot (giant priced completions), escalation storm (engineered scary text), runaway `sleep` command, crash-loop both halves (zero-budget exhaustion + disarm-resume), pre-plan hung model call (real never-responding endpoint). Measured verdicts from ledgers/journals; all 6 PASS (`logs/abuse/final-r6/`). |
| `stress.py` | **Real** | Task B harness: N tasks at target concurrency with simultaneous multi-kill; asserts completion, resume proof, cap proof, journal records, product-output (real mode), approval-gate exemption (real+approval mode); standalone (`python -m runtime.stress`), NOT in the pytest suite (spawns 40-50 real workers). |
| `soak.py` | **Real (Round 7)** | Long-duration soak harness: ONE long-lived scheduler across 120 sequential batches x 30 tasks (3,600 tasks, 5.15 simulated task-hours), 120 mid-run kills; watches RSS growth, latency p95 drift, per-task artifact flatness, journal growth, worker leaks, .tmp residue — the degradation classes short stress runs miss. 15/15 checks PASS (`logs/soak/r7-final/`); found the fsutil PermissionError race (fixed). Profiles default/quick/ci; standalone, NOT in pytest. |
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

## Test coverage (all green as of Improvement Round 2)

`tests/test_model_router.py` (21), `tests/test_difficulty_approval.py` (8), `tests/test_scheduler_integration.py` (14 — real process kills, hang detection, approval file protocol, ablation toggle, **gate-park/hang interaction**), `tests/test_ensemble.py` (11 — Improvement Round 2), `tests/test_provider_smoke.py` (Ollama 2 passed; cloud 3 self-skip), `tests/test_analyze_history.py` (38 — Cross-Task round). Runtime-module-only run at Cross-Task round close: **95 passed, 3 self-skipped, 0 failed**. Full-suite run at Round-5 closeout (all terminals' tests, post both closeout fixes): 300 passed, 3 skipped, 0 failed. Stress runs (`python -m runtime.stress`), the Round-6 multi-repo self-check (`python -m runtime.multirepo_tasks --check`, 5/5) and the abuse suite (`python -m runtime.abuse`, 6/6) are separate from pytest by design.

Definition-of-done checks, verified not assumed:
- ✅ Scheduler runs 10+ concurrent fake tasks; concurrency cap proven from event journal (`max_overlap <= cap`); parallel beats serial floor.
- ✅ Mid-task crash (hard `os._exit`) → resume from completed steps, proven by: 2 worker starts (fresh, resume), state.json all-steps-complete, attempt=2, event journal (`crash`→`crash_retry`→`finish`).
- ✅ Hang → killed via stale state.json + one-shot injection disarm → completes after resume.
- ✅ Adaptive routing toggle demonstrably changes model choice + ledger cost (ablation architecture test).
- ✅ Round 6: every budget/limit cap FIRED under deliberate worst-case triggering (abuse suite 6/6, measured verdicts — see Task B above).

## Known limitations / decisions future-you should know

1. **Windows process semantics**: `proc.kill()` on Windows is hard-kill ( TerminateProcess) — workers get no cleanup chance. That's exactly the crash scenario we checkpoint for, so it's fine (and the fake's `os._exit` matches).
2. **Hang detection granularity**: state.json mtime staleness requires the harness to touch state.json per step; if the real harness goes minutes between state writes (e.g. one long test run), raise `hang_heartbeat_stale_s` accordingly or add a step-level progress file. Measured (r4-real-45-45-8): healthy work-phase gaps up to ~98s (p95 75s) under 45-way Docker load — size the window above your real work, not just above your model calls. **Gate-parked workers are exempt from the state-stale kill while their checkpoint says `awaiting_approval` (or `finished`) and the heartbeat is fresh** — the approval park is not a hang; heartbeat death and the wall-clock cap still kill. **Round 6 abuse finding (prestate scenario): the harness's EARLY state.json write (TaskState init, pre-plan) is what makes even a planner-phase hang state-stale-detectable — if a harness change ever delays that first write past hang_heartbeat_stale_s, only the wall-clock cap bounds pre-plan hangs. Keep the early write.**
3. **PID reuse** (fsutil `is_pid_alive`): heuristic only; never used for correctness decisions.
4. **Approval gate file protocol** assumes one approver; concurrent conflicting decisions resolve last-write-wins via atomic replace.
5. **The ablation IS run** (Rounds 2-4 + Round 6's multi-repo extension): v1 honest negative, v2/v3/v4 positive (2.59-3.47x), v6-multirepo out-of-distribution confirmation (4.19x, ON +20% success at 24% cost) — all archived under `logs/ablations/` with proxy-price honesty notes. Next step for Phase 6: SWE-bench Lite subsets, real paid tiers, repetitions.
6. **`logs/` is gitignored** — all run state (ablations, stress, abuse, multirepo clone cache) lives there; nothing in-repo depends on committed artifacts. Summary stats are recorded HERE and in each run's `summary.json`/`stress_report.json`/`abuse_report.json`.
7. **Cheap-tier endpoint variance is a project-long hazard (now realized)**: nararouter's qwen3.8-27b exhausted its free credits mid-project (Round 6); stepfun-3.7-flash is the replacement (probed live; longcat-2.0-free rejected for pseudo-XML tool-call output that the bash-only harness cannot execute). If a future ablation behaves erratically, check endpoint health first (probe-scripts pattern in the Round-2 story + the Round-6 probe sequence).
8. **Budget-cap granularity is attempt-level (Round 6 abuse finding, measured)**: `over_budget()` fires at attempt START (harness core), so one full in-attempt session (up to max_step_turns calls) can burn past `budget_cap_usd` before the next check — adversarially measured at 9.28x a deliberately tiny cap, one-attempt-bounded, never unbounded. A per-call pre-check (router reads remaining budget before dialing) would tighten this to +1 call if ever needed; the attempt-level granularity was accepted as the documented trade for now (a per-call check adds a router↔harness coupling).
9. **Cheap-tier endpoint variance (history)**: nararouter (qwen3.8-27b) went from 12/12 instant replies to 90-240s/call within a single evening in Round 2 — the router's transient retry saved the v2 ON arm; the endpoint then fully exhausted its credits in Round 6 (see 7 above). Endpoint health is the FIRST thing to check when an ablation behaves erratically.
