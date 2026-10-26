# AGENTS.md — evals/: the internal prompt-regression eval harness

(Observability & Eval round, Task B. Read the root AGENTS.md and
INTERFACES.md first — this module is cross-cutting by design but owns
NO boundary; it CONSUMES harness.core.run_task, the Docker sandbox/
verifier, runtime's scripted-model mechanism, and harness config keys.)

## VEX-PF-11 — the human session journey (Terminal 11, 2026-09-30)

`evals/session_journey.py` + `tests/test_daily_session_human.py` (61 tests,
~224 s). It drives the **real `cli.tui.NeoApp`** through a Textual Pilot with a
scripted model and no Docker, no provider, no network, and no credential read.
Read this section before the rest of the file — the rest documents the
prompt/Docker lanes, and this one is a different kind of measurement.

```powershell
python -m pytest tests/test_daily_session_human.py -p no:randomly -q
python -m evals.session_journey --out-root logs/product-round/session-journey
python -m evals.session_journey --quick        # 4 scenarios, EXCLUDES the corpus
python -m evals.session_journey --diff <prior-run-dir>
```

### The one thing to understand before you touch it

**The diff is the product; the runs are just how you produce two of them.** A
gate that reports changes nobody made is worse than no gate, because people
learn to ignore it. So every step carries a `delta` — the text that step
*added* — and the diff compares that, plus the step's fields, plus the
findings. It deliberately does **not** compare `elapsed_ms` (a measurement, not a
behaviour) and does **not** compare the 4000-character transcript tail (a
sliding window that moves on every turn of a 60-turn session).

Two full runs of the same tree currently report `identical: true` across all
six scenarios and 143 typed lines. Getting there took four fixes, and **all
four are worth knowing about before you "simplify" any of them away**:

| artefact | what it looked like | the fix and why it is not a convenience |
|---|---|---|
| sliding tail | 93 of 111 diff hits were the 4000-char window moving | the tail stays as reader context and left the diff; the delta is the signal |
| lost wrap space | segment stream read `change codein this repo` where the terminal draws `change code in this repo` | `_repair_wrap_spaces` consults the **frame** (the rendered truth) and only ever adds a space the frame proves is on screen. A token with no frame-backed split is untouched, so a real content change is still reported |
| card chrome | the card measures its columns against a rail still tearing down, so the same run drew different rule lengths and wrapped the id at a different column | `normalise_box_chrome` strips box drawing. The verdict words are *text*, not box drawing, and survive — and a test asserts both halves together, because a rule surviving where a verdict is read is exactly what `last_verdict` exists to prevent |
| repaint race | the run line repaints on a 0.125 s timer and the journal tail is a thread, so a capture taken the instant a step returns missed rows a frame later | `_settle` waits 200 ms and pauses the pilot twice. ~29 s across 143 steps, and it is the reason the delta is stable |

### One step is exempt from the diff, and it says so

`cancel_and_resume#1` is typed and cancelled in the same breath, so whether the
journal tail got to render that run's feed lines before the cancel landed is a
scheduling fact, not a behaviour. `JourneyStep.diffable = False` publishes it;
`diff_runs()` lists it in `racy_steps` and every affected row carries a
`skipped_reason`. A test asserts the exemption exists, is at most 2 steps, and
names a `cancel`/`resume` step — silently exempting a step would be a gate
quietly measuring less than it claims.

### The 69-phrase corpus is the intent router's regression net

`PHRASE_CORPUS` is 69 utterances and `RECORDED_ROUTING_GAPS` is **13** of them
(both counts were previously mis-stated as 74 and twelve in the docstrings;
they are now derived and asserted). The gate compares **tier-1 verdict → the
dispatch the shell actually made**, not verdict → verdict, so it also proves no
greeting quietly started a run. The 13 gaps are pinned **inverted**: a test
asserts the dispatch the tier does *not* expect is the one the shell made, so
fixing the router in `harness/agent_loop.py` fails that test on purpose. Treat
`RECORDED_ROUTING_GAPS` as the work list — 13 names, one assertion each.

**`--quick` deliberately excludes `phrase_corpus`** (the `daily_session` /
`cancel_and_resume` / `unverified` / `hostile` subset is the quick lane). A fast
lane that skipped the corpus would have skipped the net, and a test asserts the
corpus is not in `QUICK_SLUGS`.

### What this module does NOT claim

The backend is scripted and deterministic **on purpose** — a gate whose output
moves with a provider's mood cannot be a gate. So this measures the **shell**,
not model quality. Every status in every receipt was published by the fake
backend; no assertion derives or promotes one, and a test asserts that. There
is no verifier gate here and no real Docker; the real Docker matrix is a
different lane. `unverified_is_not_success` is the one that matters most: a
`completed_unverified` run must never read as success, and the scenario fails
if a receipt ever claims verified from an unverified status.

### Receipts

`manifest.json` per run holds the SHA-256 and byte count of all 12 artifacts
(6 SVG + 6 transcript); all 12 re-hash correctly from disk. **Both** the SVG and
the text are path-normalised first, because the shell's wordmark prints the
repository and log roots and an un-normalised receipt differs between two
identical runs by a random temp directory name. `global_state_restore_failures`
is named for what it **holds** — failures — because the earlier
`global_state_restored: []` read like a list of things restored, and an empty
list of successes invites the opposite conclusion. It is asserted `[]` for every
scenario.

### Two real defects found, REGISTERED, not edited

`cli/tui.py` and `cli/runview.py` are other terminals' files and this round
edits no product module, so both are registered with an owner, a location, and
a suggested fix rather than papered over:

- `resume_produced_no_visible_output` — `/resume` **dispatches** a real run but
  the transcript grows by zero characters. Owner `cli/tui.py`
  (`_resume_worker` / `_note_result` / `_finish_run`).
- `rail_calls_a_mapped_row_unreadable` — the rail calls a row unreadable
  (`plan`, `verify_skipped`) while the transcript rendered a sentence for the
  same journal row. Owner `cli/runview.py::EVENT_VOCABULARY`.

Both are asserted to have been **actually observed**, so the register cannot rot
into a list of aspirational bugs.

## VEX-CEILING-08 — verification-intelligence feature lane (2026-09-26)

The feature-evidence registry is now **14 features x 2 arms = 28 arms**. The
new entry is `verification_intelligence`, and it is the first feature lane in
this module that drives the **real Docker verifier**.

### The probe

`evals.daily_driver._feature_verification_intelligence` builds a small real
repository (`app.py`, `other.py`, `tests/test_app.py`, `tests/test_other.py`)
and then runs `execution.verification_intelligence.run_verification` twice per
arm: once against a **tampered** sealed spec and once against an **intact** one.

| receipt (baseline) | meaning |
|---|---|
| `verification_intelligence_spec_refused` | a deleted spec item is a FAILING `spec_intact` gate, with the item named in `removed_items` |
| `verification_intelligence_intact_tree_verified` | the intact tree mints success with the independent judge accepted |
| `verification_intelligence_disabled` | false on baseline, true on the off arm |

Semantic assertions: the final gate's scope is `full`; the import-graph
selection is genuinely incremental (picks `tests/test_app.py`, excludes
`tests/test_other.py`); the selection is persisted with the run; an
unconfirmed failure is **not actionable**; and an intact tree mints success
**only** when the intelligence is on (baseline additionally requires
`judgment.verified is True`; the off arm requires `judgment is None`).

### Why the adversarial arm has NO spec

This is the part worth copying. The off arm runs the same real gate with
`require_spec=False` **and no spec artifact on disk**, so its `spec_intact` gate
is `skipped` rather than `failed`. An off arm that still refused a deleted spec
would prove nothing — the refusal could have come from anywhere. Because the
off arm has nothing to refuse, the baseline arm's refusal is attributable to the
gate rather than to the fixture.

### Two real defects the lane found

1. **A crash was being recorded as a failing test.** The judge ran inside the
   sandbox and hit `OSError: [Errno 5] Input/output error`; the old parser read
   that as `exit 1` -> "1 failed" -> a rejected claim. That is an environment
   fault dressed as a product regression, and it would have been exactly the
   kind of fake evidence this module exists to reject. `parse_test_run` now
   classifies a crash-shaped capture with no test counts as `error`.
2. **A deeply nested judge path breaks Docker Desktop bind mounts.** The judge's
   independent copy originally lived under the run directory; at eval-tree depth
   every read inside the mount returned `EIO`, which pytest surfaces as a
   traceback from `locate_config`. `execution.verification_intelligence` now
   stages the judge's copy in a SHORT system-temp directory. Fixing this took
   the quick-matrix test from a 130s failure to 67s green.

### The probe is honest about cost, and so is the budget

The probe runs the real Docker verifier for both arms (~10 container runs
total), so it is the most expensive feature in the lane. `_run_feature_lane`'s
default wall-clock budget moved **240s -> 900s**. A 240s budget made the whole
lane report `failed` for a TIMEOUT rather than for a defect, which is precisely
the "a skipped or blocked lane is not a pass" failure mode this harness exists
to avoid — inverted into "a timed-out lane is not a pass for the wrong reason".

The judge's sandbox run is retried **once** and only when the outcome is
`error`, using this round's own flake policy: a crash is not a failing
acceptance test. A genuine test failure is never retried. Both attempts' raw
container output is retained in the probe evidence, because "the judge said no"
without the reason is not evidence.

### Verification (this tree)

- `python -m pytest tests/test_daily_driver_evals.py tests/test_evals_run.py
  tests/test_evals_tasks.py -q -p no:randomly` -> **69 passed**.
- `python -m evals.run --suite daily-driver --check` -> feature evidence
  `complete`, **28/28 arms passed**; 24 case arms (23/24 ok, the one failure
  being `dd_20_live_tui_status_diff`, a Textual TUI case owned by the terminal
  UX round, not this one). Verdict `ERROR` / `ready=false` is reported
  honestly because Docker and the live-provider lane were not selected for full
  readiness.
- `python -m evals.run --suite prompt-regression --check` -> **14/14 CLEAN**.
- **No live-provider lane was run and no credential was inspected.**
  `verification_intelligence` is a deterministic Docker-backed lane; it is not
  a model-quality measurement and does not contribute to any live quality
  number.

### Change for other terminals

- `tests/test_daily_driver_evals.py::test_feature_evidence_lane_covers_every_active_feature`
  no longer hardcodes `26`. It now derives the count from
  `len(FEATURE_EVIDENCE_SPECS)` AND pins that length to `14`, plus asserts
  `len(ACTIVE_PROMPT_FEATURES) == len(FEATURE_EVIDENCE_SPECS)`. That keeps a
  real pin while removing the duplicated arithmetic that broke the moment a
  feature was added.
- Anyone adding a feature must add BOTH an `ActivePromptFeature` and a
  `FeatureEvidenceSpec` with a `probe`; the test now fails loudly if the two
  registries diverge.

## Terminal 15 — measured evidence, SLOs, and quality gates (2026-09-25)

This round replaced self-certifying readiness with measured evidence. The
gap that motivated it: `_aggregate` handed `quality_capability_coverage`
a document of literal `True` values, and the capability check only tested
for **key presence** — so a caller could satisfy a quality capability by
writing booleans. That is now closed, and the module gained four
measurement subsystems.

### 1. Quality capability coverage is computed from measured values

`quality_capability_coverage` is unchanged for the 16 case-backed
capabilities, but the `aggregate` capability
(`cost_latency_user_intervention_quality`) now requires **measured
aggregates with provenance**:

- `_measured_quality_summary(results, metric_documents=..., manual=...)`
  derives `cost_usd_total`, `latency_ms` (p50/p95), `token_total`,
  `observed_user_intervention_count`, `manual_corrective_follow_up_count`,
  and `required_receipt_coverage_rate` from observed arm/lane metrics, and
  records `measured_from` (the contributing case ids).
- `_is_measured_metric` rejects `bool` outright, and treats a percentile
  block as measured only when `value`/`mean`/`p50`/`p95`/`min`/`max` holds a
  real number. `{"samples": 0, "p50": None}` is **not** a measurement.
- A missing contributor means `None`, not `0` — `_sum_metric` over zero
  cost-bearing documents reports `cost_usd_total: None` rather than `0.0`.
- Coverage additionally requires non-empty `measured_from` provenance and at
  least one observed result. A row's `reason` names the exact gap.

Deleting any one of the six fields makes the capability uncovered, which
makes `readiness.required_quality_capabilities_observed` false.

### 2. `evals/slos.py` — real SLOs measured from artifacts

Nine measured objectives (the eight required categories; fix latency is
tracked at p50 and p95), each with a direction, a target, and the input
metrics it consumes:

| key | target |
|---|---|
| `verified_success_rate` | `>= 0.90` |
| `fix_latency_p50_ms` | `<= 300000` |
| `fix_latency_p95_ms` | `<= 900000` |
| `cost_per_task_usd` | `<= 0.025` |
| `tokens_per_task` | `<= 120000` |
| `user_interventions_per_task` | `<= 0` |
| `sandbox_command_latency_p95_ms` | `<= 120000` |
| `context_utilization_p95` | `<= 0.80` |
| `provider_fallback_rate` | `<= 0.10` |

The anti-self-certification mechanism, in order of application:

1. **Provenance.** `collect_measurements` records `artifact` (a real file
   path) and `artifact_sha256` for every run. `measure_slos` re-reads and
   re-hashes each artifact and refuses any record whose digest no longer
   matches, that lives outside the measured logs root, that is a symlink, or
   that carries no artifact at all. Refusals are reported, never dropped.
2. **Measured values only.** `bool` is never a number (`_is_number`
   rejects it). A record whose `metrics` are `True` cannot satisfy an SLO.
3. **Fail closed on a thinning denominator.** An SLO is measured only when
   *every* accepted run reports *every* input it consumes. One run that
   stopped emitting a receipt makes the SLO `unmeasured` — never a pass
   computed on a smaller sample.
4. **No evidence is not zero.** With no accepted records every SLO is
   `unmeasured`, not `met` at 0.

`verified_success_rate` counts only `completed_verified` (the
verifier-minted status), with a Wilson 95% interval and the derived
`eval_noise_band` published alongside so a nightly diff is not over-read.
`_user_interventions` returns `None` — not `0` — when a run produced no
receipts at all, because "no evidence" and "zero interventions" are
different answers.

Verdict vocabulary is deliberately three-valued:
`MEETS_SLOS` / `MISSES_SLOS` / `INSUFFICIENT_EVIDENCE`.

```powershell
python -m evals.slos --logs-root logs --json
python -m evals.slos --logs-root logs --require-ready   # exit 2 otherwise
```

**Honest status in this checkout (2026-09-25):** run against the real
`logs/` tree, `python -m evals.slos --logs-root logs` reports
`INSUFFICIENT_EVIDENCE` with 44 measurements accepted and **all nine** SLOs
unmeasured. The existing `logs/{task}` artifacts are runtime bench
directories that do not carry the `model_routed`/sandbox/context receipts
this gate needs. That verdict is correct: this checkout has no live-run
evidence for these SLOs, and the module says so instead of reporting zero.

### 3. `evals/live_quality.py` — nightly live multi-provider matrix

- 20 fixed real tasks (`LIVE_QUALITY_TASKS`, `MIN_MATRIX_TASKS = 20`) drawn
  from this repository's actual code, each with grounding markers a correct
  answer must actually cite, plus mined regression cases appended from
  `evals/regression_cases.json`.
- `summarize_live_matrix` records `n`, `providers`, `provider_count`,
  `success_ci` (Wilson), `eval_noise_band`, `cost_per_task` /
  `cost_per_task_usd`, `tokens_per_task`, a per-provider breakdown, and the
  four judged dimensions. `meets_matrix_minimum` is true only at >= 20 real
  tasks across >= 2 providers.
- The judge scores `correctness`, `minimality`, `explanation_quality`, and
  `user_acceptance`. `judge_sample(task, answer, changed, judge_call=None)`
  uses a live judge when one is supplied and otherwise an evidence-based
  deterministic rubric that is always labelled
  `judge_mode="deterministic"`, `live_judge=False` and counted separately in
  `live_judged_samples` / `deterministic_judged_samples`. A deterministic
  score can never be reported as a live quality result.
- A sample counts as a success only with an accepted status **and** a judged
  `correctness >= 0.7`.
- `preflight` resolves provider targets without reading or serializing a
  credential value (`credential_values_read: false`). Without a usable
  target `run_live_matrix` returns `status="blocked"`, `verdict="NOT_RUN"`,
  `matrix=None`, and mines nothing.
- `mine_failures` + `record_failures` append every observed failure to
  `evals/regression_cases.json` (committed, reviewable, idempotent per
  `task_id|provider|reason` fingerprint). `live_task_set()` folds the
  registry into the nightly task set, so a real failure becomes a permanent
  regression case.

```powershell
python -m evals.live_quality --providers openai:gpt-4o,anthropic:claude-haiku
python -m evals.live_quality --summarize samples.json --json
python -m evals.live_quality --mine samples.json --registry-root . --json
python -m evals.live_quality --summarize samples.json --require-matrix  # exit 2
```

### 4. `evals/ci_truth.py` — the CI truth gate

Six checks, each a hard failure, driven by a line-oriented workflow parser
(no YAML dependency):

| check | meaning |
|---|---|
| `every_test_file_covered` | every `tests/test_*.py` runs in a workflow or is on `TEST_ALLOWLIST` |
| `full_suite_in_release_gate` | the release verdict depends on a lane that runs the whole `tests/` tree |
| `required_checks_declared` | every `REQUIRED_PR_CHECKS` name is provided by a `pull_request` workflow |
| `security_lane_in_pr_path` | a PR workflow runs the security regressions |
| `flake_lane_covers_timing` | every `TIMING_SENSITIVE_TESTS` file is repeated by a lane |
| `manual_checks_are_manual` | opt-in live lanes are never required PR checks |

The coverage rule is deliberately strict: a bare `pytest tests/` invocation
counts a file as covered **only** when that job is in the release
aggregate's `needs`. A full-suite run nobody requires is exactly the G37
gap, so it is not coverage. `python -m evals.ci_truth --check` exits 2 on
any gap.

```powershell
python -m evals.ci_truth --check --json
```

### 5. `evals/evidence.py` — committed, sanitized evidence bundles

`logs/` is gitignored, which is right, but it also made every quality claim
unreviewable. A bundle is the committed counterpart: a small sanitized
distillation of a run's real artifacts under
`evidence/bundles/<label>-<ts>/`, holding `manifest.json`, `slo_report.json`,
`ci_truth.json`, `quality/*`, `spans/<task>.json`, and `spans/otlp.json`.

Sanitization is three layers, because each alone leaked on real data:

1. `shared.privacy`'s `shareable` view (content, paths, identities).
2. `_scrub` — a **deep** walk that drops `_DROP_KEYS` (prompts, answers,
   messages, diffs, commands, stdout/stderr, host paths, trace paths) and
   rewrites path-shaped strings to a portable label. The deep walk exists
   because a real `daily_driver_report.json` nests transcripts far below the
   key set the privacy view knows about.
3. `_sanitize` adds `shared.security.redact_secrets`, and any string longer
   than `MAX_VALUE_CHARS` (300) becomes `[redacted free text: N chars]` —
   free text may quote a prompt or source, and it is not evidence. Numbers,
   statuses, digests, counts, and verdicts are all preserved.

`verify_bundle` re-reads the bundle from disk and fails on a digest
mismatch, a missing file, an unparseable file, a surviving `_DROP_KEYS`
key, an un-redacted value under a sensitive key, an un-redacted token under
an innocuous key, a nested absolute path, or surviving free text. The scan
is **structural** (parsed JSON, key/value pairs), not raw-text: a raw scan
cannot tell a Windows path from a JSON escape (`k:\\nTOOL` reads as
`C:/x`) and cannot tell a content key from a content value — both mistakes
produced false alarms on a real build, and a gate that cries wolf is a gate
people stop reading.

`purge` deletes **whole bundles** (a bundle is the unit a reviewer reads),
selecting by `keep_latest` or `max_age_s`, delegating the file deletion to
`shared.retention.apply_retention` (so containment, symlink refusal, and byte
accounting stay shared rather than forked), pruning emptied subdirectories,
and appending one redacted `purge_receipts.jsonl` receipt.

```powershell
python -m evals.evidence build --logs-root logs --out evidence/bundles --label release
python -m evals.evidence verify evidence/bundles
python -m evals.evidence list --root evidence/bundles
python -m evals.evidence purge --root evidence/bundles --keep-latest 5 --dry-run
```

### 6. Workflow changes (`.github/workflows/**`)

- **New `eval-matrix.yml`** — the required PR path. Jobs:
  `ci truth gate` (runs `evals.ci_truth --check` plus the hostless eval and
  observability regressions), `hostless security regressions` (declared in
  `REQUIRED_PR_CHECKS`; a skipped security test is parsed out of the JUnit
  XML and fails the lane rather than passing), `prompt-regression-matrix`
  (the declared required check: host `--check`, the real paired-arm matrix,
  and the deterministic daily-driver matrix), and
  `timing-sensitive flake lane` (repeats every `TIMING_SENSITIVE_TESTS`
  file three times).
- **New `nightly-quality.yml`** — scheduled/manual only, deliberately absent
  from the PR path because it needs real credentials and real spend. Runs
  the live matrix (`--require-matrix`), mines failures, measures SLOs,
  builds and verifies the evidence bundle, and uploads sanitized evidence.
  A blocked lane is reported as blocked; `evals/ci_truth` fails if a
  manual-only lane is ever promoted to a required check.
- **`release-gate.yml`** — added a `full-suite` job that runs
  `python -m pytest tests/`, requires >= 500 executed tests from the JUnit
  report (a collect-only count is not a suite), and runs `evals.ci_truth
  --check`; it is now in the `release-gate` verdict's `needs` as
  `FULL_SUITE`.

### Terminal 15 verification (2026-09-25)

- `python -m pytest tests/test_slos.py tests/test_live_quality.py
  tests/test_ci_truth.py tests/test_evidence_bundles.py
  tests/test_ceiling15_spans.py tests/test_quality_capability_gate.py
  tests/test_tracing.py tests/test_release_workflows.py -q -p no:randomly`:
  **127 passed**.
- `python -m evals.ci_truth --check`: **CI_TRUTHFUL**, all six checks ok.
- `python -m evals.run --suite prompt-regression --check`: **14/14 CLEAN**.
- `python -m evals.slos --logs-root logs`: **INSUFFICIENT_EVIDENCE** (see
  above — this is the honest verdict for this checkout, not a pass).
- `python -m evals.evidence build --logs-root logs`: **169 files, verification
  ok: True** on the real (unmodified) `logs/` tree.
- `python -m ruff check evals/ shared/tracing.py shared/traceview.py
  tests/test_{slos,live_quality,ci_truth,evidence_bundles,ceiling15_spans,quality_capability_gate}.py`:
  clean.
- **BLOCKED (not passed):** no live-provider lane was run. This checkout has
  no configured model or credential, so `run_live_matrix` returns `blocked`
  and no live quality number is claimed. The nightly matrix is the lane that
  produces that evidence.
- **BLOCKED (cross-terminal, not mine):**
  `tests/test_daily_driver_evals.py::test_new_daily_quality_cases_pass_both_arms[dd_25_checkpoint_hard_kill-*]`
  fails with `hard_kill_observed: False`. Root cause is
  `harness/agent_kernel/kernel.py:132` — `NameError: name
  'resolve_agent_strategy' is not defined` — a harness-module break in
  Terminal 01's in-flight work. Terminal 15 owns neither file and did not
  edit them. The remaining 67 tests in that selection pass.

### Terminal 15 — not yet implemented / deliberate scope

- No live-provider quality matrix has been executed. The runner, the judge,
  the registry, and the nightly workflow exist; the *result* is blocked on
  credentials, and no substitute number is reported.
- `evals/slos.py` has never been run against a run layout that carries
  `model_routed`, `sandbox_call/result`, and `context_budget` receipts, so
  the SLO targets above are **proposed**, not calibrated. The first real
  measurement should be reviewed against them before they become promises.
- `evidence/bundles/` is unignored in `.gitignore` but **no bundle is
  committed yet**; the nightly lane produces them. Committing one is
  Terminal 17's dogfood step, not this terminal's.
- `TEST_ALLOWLIST` is empty by design: with the new release-required
  full-suite lane, all 92+ test files are covered. Adding an entry is a
  deliberate, reviewed decision with a written reason.
- `evals/run.py` and `evals/__main__.py` were deliberately **not** edited —
  they are another terminal's in-flight files. The four new subsystems are
  reachable through their own `python -m evals.<module>` entry points.

**2026-09-13 update (web-fetch round): the task set is now 14 and the
arm set 8 — `eval_fetch_webpage` (scenario) + the `no_webfetch` arm
landed with harness/webfetch.py, and `eval_skills_injection` +
`no_skills` carry the skills round. Prior eval reports remain comparable
per-task; only the new task/arm have no history. The
`web_fetch_enabled` and `skills_enabled` keys joined `_ROUND_KEYS`
(`pre_round` turns both off). eval_fetch_webpage's scripted fix does
NOT depend on the fetched page's content — the scenario guards loop
machinery (intercept + reinject + continue), preserving determinism on
any fetch outcome; the fetched-content proof lives in
tests/test_webfetch.py instead (Docker+net gated).**

## Terminal 10 daily-driver quality round (2026-09-25)

The Master Prompt 10 matrix is now a fixed **26-case × 2-arm** suite with a
separate 17-capability coverage contract. The original 20 cases remain
unchanged; six cases were added:

- `dd_21_repair_broken_test`: repairs a broken test without weakening it and
  requires explicit ACI feedback before the corrective action.
- `dd_22_project_instructions`: proves root, nested, and `.neo` instruction
  discovery, small-budget preservation, and model-context use.
- `dd_23_large_repo_map`: ranks a 140+ file synthetic repository through the
  product retrieval/code-graph path, rejects distractors, and checks a stable
  token-bounded result with citations.
- `dd_24_long_session_continuity`: forces repeated structured compaction and
  checks old-fact recovery, recent-fact retention, and no repeated mutation.
- `dd_25_checkpoint_hard_kill`: kills a worker process after an edit/checkpoint,
  relaunches with the stored token, and checks the restored diff and trace.
- `dd_26_lsp_diagnostic_repair`: starts a real stdlib JSON-RPC subprocess,
  opens a document through the public `harness.lsp.LspManager`, consumes its
  normalized `Diagnostic` through `harness.context_compiler`, repairs the
  reported finding, sends `textDocument/didChange`, verifies that diagnostics
  clear, and shuts down. It is not an AST-lint probe; the existing
  `harness.lint`/`lint_gate` path remains separate.

The runner now fails closed on empty/wrong-type task and arm selections,
missing baseline, zero **observed** comparisons, any failed selected arm,
partial matrix selection, omitted Docker/provider readiness lanes, and hidden
lane skips. Reports separate selected comparisons from valid observed
comparisons, and full readiness requires all 26 cases, both arms, feature
evidence, real Docker, real provider, all 17 capabilities, and explicit manual
evidence. A passing subset is `NOT_READY`, never full readiness.

The fixed prompt task set is unchanged at 14, but `eval_repair_retry` and
`eval_lint_undefined` now declare feedback contracts. The eval-owned model
refuses to emit the corrective action unless the exact previous-attempt
diagnostic is present in its messages; a unit negative control proves missing
feedback triggers the safe refusal. Prompt runs also hash every task source
before/after execution and fail if the benchmark mutates a source repository.

A real-provider worker is explicit and required
(`--live-provider --provider-model ... --provider-key-env ...`). Credential
values are forwarded only to that selected child and are never serialized.
Provider metrics join the aggregate report. This checkout has no configured
model, so the worker honestly returned `blocked`; no provider quality result
is claimed.

Verification on 2026-09-25:

- `python -m pytest -q tests/test_daily_driver_evals.py tests/test_evals_run.py tests/test_evals_tasks.py`: **69 passed**.
- `python -m evals.run --suite daily-driver --no-docker --json`: **52/52 arms
  passed**, 26/26 valid comparisons, 26/26 feature-evidence arms passed; verdict
  `NOT_READY` because Docker/provider were omitted, LSP is blocked, and manual
  evidence is incomplete. Report:
  `logs/evals/20260925-011833-aa507dd3f5024b099c430f1b967beb36/daily_driver_report.json`.
- `python -m evals.run --suite prompt-regression --check --json`: **14/14 CLEAN**.
- Real Docker ACI comparisons, baseline + no_lint, both clean:
  `eval_lint_undefined` at
  `logs/evals/20260925-010822-7f2ea1ec90ff40dc8410a0f1bb53683f/eval_report.json`
  and `eval_repair_retry` at
  `logs/evals/20260925-011346-4c192266d7924eed86a80628fc972555/eval_report.json`.
- Real Docker daily canary: `completed_verified`, clean target/regression/flake
  evidence, 3 model calls, 60 tokens, $0.0003.
- `python -m ruff check evals tests/test_daily_driver_evals.py tests/test_evals_run.py tests/test_evals_tasks.py`: clean.
- `ruff format --check` reports existing whole-file formatting debt in the
  already-dirty shared files; no broad reformat was applied.

## What this module is

The shared CLI also hosts the deterministic daily-driver lane selected by
`--suite daily-driver` and the opt-in combined check selected by
`--suite combined --check`. The default and compatibility `auto` path is
the fixed prompt-regression matrix; daily-driver never contaminates the
standard prompt gate unless explicitly selected.

### Daily-driver matrix (2026-09-25)

`daily_driver.py` is a fixed 26-case product-readiness matrix with paired
`baseline` and `adversarial` arms. Every case runs in a fresh subprocess
whose `HOME`, config, memory DB, logs, trace root, plugins, and network
proxies are private to the run. Provider credential variables are not
copied into child environments. Each arm persists `result.json` and a
normalized `trace.jsonl`; the suite persists
`logs/evals/<run-id>/daily_driver_report.json` with assertions, receipts,
reproducers, status counts, latency/cost/token metrics, readiness, and
explicit Docker/live-provider lanes.

```powershell
# 12-case quick fixture gate, no Docker
python -m evals.run --suite daily-driver --check
python -m evals.run --suite daily-driver --quick

# full 26 x 2 matrix + real Docker-backed canary
python -m evals.run --suite daily-driver --json

# full matrix + explicit real-provider canary
python -m evals.run --suite daily-driver --live-provider --provider-model <model> --provider-key-env <ENV_NAME> --json

# old prompt task check + daily quick, with a separate combined report
python -m evals.run --suite combined --check

# one exact case/arm
python -m evals.daily_driver --case dd_19_skill_discover_show_inject --arm baseline --case-root <new-temp-dir>
```

The fixed cases are: symbol explanation, dirty-repository edit, multi-file
refactor, test-failure interpretation, fix-loop skill content, connector
failure honesty, cross-turn continuity, resume, unsafe-write refusal,
mutation approval, cancellation cleanup, completed-unverified policy,
failed/flaky verification, stale-edit preservation, corrupt-session
recovery, provider/router precedence, first-run scaffold, plugin
lifecycle, daily skill discover/show/inject, and live TUI status/diff.
These 20 compatibility cases plus the six Terminal 10 additions listed above
form the fixed 26-case matrix. Required receipts are fail-closed and no skipped
or blocked lane is counted as a pass.

Latest full deterministic run (2026-09-25):
`logs/evals/20260925-011833-aa507dd3f5024b099c430f1b967beb36/daily_driver_report.json`

- 52/52 case arms passed; baseline completion 26/26 (100%); 26/26 valid
  comparisons.
- Feature evidence: 26/26 arms passed; 16/17 quality capabilities covered.
- Verdict: `NOT_READY`, correctly: Docker/provider were deliberately omitted,
  and sampled manual-repair evidence is incomplete. The LSP capability is
  now observed by the public-boundary probe; two required infrastructure
  lanes remain explicit skips, not passes.
- Cost $0.0246; 5090 aggregate fixture/feature tokens. Zero false verified
  successes, unauthorized mutations, lost edits, permission failures, resume
  failures, or UI thread stalls.
- A separately selected real Docker canary passed with clean
  target/regression/flake evidence, 3 model calls, 60 tokens, and $0.0003.

The report intentionally leaves `ready=false` when any required lane is
not selected or not verified. The active-feature evidence lane now runs a
fixed 13-feature x 2-arm product probe matrix in an isolated worker. Each
feature records both arm results, required receipts, semantic assertions,
evidence, trace path, and reproducer; a blocked/crashed arm is never counted
as coverage. `prompt_feature_coverage` is observed-data driven and remains
incomplete when called without that evidence document.

The sampled real-development/manual-repair gate uses
`load_manual_repair_evidence`, an explicit JSON source loader. It requires
verified samples with a boolean manual-repair observation, reports the
source, sample count, and no-manual-repair rate, and fails closed when the
source is absent or lacks that observation. The existing
`logs/oss-round6/multi_repo_report.json` is inspected as a source when
present, but is not treated as proof of zero manual repair without an
explicit field. The live-provider lane is explicit and required; when omitted
it is `not_selected` and keeps full readiness false. Missing provider
configuration produces `blocked`, never a pass.

A small, FIXED evaluation task set + a paired-arm runner that answers
one question quickly: **did a prompt (or prompt-adjacent config) change
break anything that used to work?** Same discipline as
runtime/ablation.py's paired arms, applied to prompt engineering instead
of the routing mechanism.

## How to run it (the standard pre-ship gate for ANY prompt change)

```powershell
# host self-check of the task set itself (no Docker, ~30s):
python -m evals.run --suite prompt-regression --check

# quick gate — fixture tasks only (fast):
python -m evals.run --suite prompt-regression --quick

# the full matrix — 14 tasks x 8 arms, real Docker loop (~18 min):
python -m evals.run --suite prompt-regression

# just some tasks / arms (dev loop):
python -m evals.run --suite prompt-regression --arms baseline,no_lint --tasks bug02_mean,eval_lint_undefined

# machine-readable (CI):
python -m evals.run --suite prompt-regression --json     # exit 0 = CLEAN, exit 2 = REGRESSIONS/ERROR
```

Docker must be up for the arms (the verifier is real); `--check` and
`--quick`'s host checks are Docker-less. Each invocation creates a
unique run directory under `logs/evals/` and writes its report at
`logs/evals/<run-id>/eval_report.json` (+ per-arm/per-task log trees
with full traces under that run).

## The task set (14, fixed — changing it invalidates comparisons)

| slug | source | what it guards |
|---|---|---|
| bug01_wrap … bug05_cart | the 5 original fixtures (tests/fixtures/) | the DoD set every e2e suite pins — comparability across project history |
| eval_strip_boundary | synthesized | string-boundary bug class |
| eval_wrong_operator | synthesized | wrong-operator class |
| eval_wrong_constant | synthesized | wrong-constant class |
| eval_lost_guard | synthesized | lost-exception-guard class |
| eval_repair_retry | scenario | the REPAIR loop: attempt 1 lands a SYNTAX error, attempt 2 fixes it (requires attempts>=2 — feedback must carry) |
| eval_docs_lookup | scenario | the DOCS escape: mid-step DOCS lookups must not break the loop (both docs-on/off arms green) |
| eval_lint_undefined | scenario | the LINT gate: attempt 1 is an UNDEFINED NAME (valid syntax — only the lint gate sees it), attempt 2 fixes it |
| eval_fetch_webpage | scenario | the FETCH escape: a mid-step real web fetch (intercept + reinject + continue) must not break the loop (both fetch-on/off arms green) |
| eval_skills_injection | scenario | a matching skills scan must produce a receipt on baseline and a disabled receipt on no_skills without breaking the loop |

Synthesized/scenario repos are REBUILT FRESH under the unique run's
`repos/` directory (`all_tasks()` is deterministic; test-pinned
byte-identical). Fixture repos are read-only committed trees. The
runner never reuses a shared `logs/evals/repos/` tree.
**Every repo is written with explicit LF newlines** — the scripted
fixes are seds that run in the LINUX sandbox, and a CRLF file breaks
`$`-anchored sed expressions inside the container while still passing
host-side checks (the exact false-negative that cost a debugging cycle;
see "Bugs found by its own harness" below).

## The arms (config dicts, NOT code forks)

`baseline` = everything on (what ships). `no_memory` / `no_lint` /
`no_docs` / `no_agent_tests` / `no_webfetch` / `no_skills` = one
improvement-round feature off each (`no_lint` disables both `lint_gate`
and its dependent `lint_names` pass). `pre_round` = the whole round off
(pre-improvement prompt surface). The `_ROUND_KEYS` list in run.py MUST
stay in sync with harness/config.py defaults — test-pinned
(`test_runner_arms_use_real_config_keys`). Every `_ROUND_KEY` must have
an arm whose override dictionary contains exactly that key, plus a
false entry in `pre_round`; the report exposes this as machine-readable
`feature_coverage`. Structural drift in a registered round arm is fatal.
The legacy prompt-regression report still lists `self_critique` as an
uncovered round key until that suite has its own ablation task. The daily-
driver feature-evidence lane independently exercises the active self-
critique product path and reports its observed receipt.

## Scoring (per task x arm)

- `status/verified`: from the REAL TaskResult (target + regression +
  not-flaky through the real Docker verifier)
- `attempts`: retry count (repair/docs/lint scenarios REQUIRE >= 2 —
  their whole point is the feedback loop carrying)
- `integrity`: machinery checks on the task's own trace.jsonl —
  task_start AND task_end AND result present; no plan_parse_error;
  no nudge loop (>3 no-command nudges); files_touched recorded
- `receipts`: task/arm-specific trace receipts; a missing, mismatched,
  or forbidden receipt makes that result `ok: false` and prevents a clean
  verdict. The DOCS, FETCH, lint, and skills scenarios require their
  enabled-arm event and forbid that event in the matching disabled arm.
- A REGRESSION = any task/integrity check that worsens vs baseline;
  the report names the exact moved check. Timing is reported, never
  gated (Docker warm/cold noise).

## Determinism notes (honest)

Scripted models make replies identical across arms (offline, zero
network); the loop, sandbox, and verifier are REAL. Wall-clock varies
with Docker warmth — that's why timing never gates. The memory arm gets
an ISOLATED pre-seeded decision store (two relevant + one noise row)
under the run directory. `NEO_TRACE_DIR` and `HARNESS_DECISIONS_DB` are
snapshotted and restored exactly, including unset prior values.

## Bugs found by its own harness (the meta-result — keep these)

1. **Two-command script replies silently drop the second command.**
   The step contract is ONE bash command per turn; `_extract_command`
   beheads multi-command replies. The original eval_lint_undefined
   attempt-2 script was two seds — the real fix sed was dropped and the
   task failed with a confusing verify error. Now test-pinned:
   `test_scripted_replies_are_single_commands` in
   tests/test_evals_tasks.py fails any multi-line non-fenced scripted
   reply at task-set build time.
2. **CRLF repos false-pass host checks, false-fail in-sandbox.** The
   `$`-anchored sed `s/return status$/return status.lower()/` matched
   nothing inside the Linux container (`return status\r`), while host
   Git-Bash sed tolerated CRLF — so `--check` was green and the real
   loop failed. Fixed by LF-forcing `_build_repo` (+ dropping the
   anchor) and pinned by `test_rebuild_is_deterministic`.
3. **Shared-repo pollution false-pass.** Before the rebuild-fresh fix,
   a prior run's fix sat in `logs/evals/repos/<slug>/` and the next run
   "passed" pre-fix (baseline verify green, zero attempts). Now every
   run rebuilds and `test_synthesized_repos_are_buggy_pre_fix` runs the
   target host-side on the built repo.

## Unified tracing integration

`run_eval` points `NEO_TRACE_DIR` at the unique run root and restores
its prior value at exit; `_run_one` re-points it per task
(`<arm>/<slug>/_trace/`) so arms never interleave one task id's stream.
Reconstruct any task's full lifecycle (arms + scenarios included):

```powershell
python -m shared.traceview <slug> --logs-root logs\evals\<run-id>\<arm>
```

## Files

| File | Role |
|---|---|
| `tasks.py` | the fixed task set: fixtures + synthesized + scenarios; `all_tasks()`, `check_set()` (host self-verify) |
| `run.py` | the paired-arm runner: ARMS, `_run_one` (real loop + scoring + integrity), `run_eval`, CLI (`--suite/--check/--quick/--arms/--tasks/--json`) |
| `daily_driver.py` | 26-case deterministic matrix, 17-capability coverage, isolated workers, real Docker/provider lanes, and fail-closed readiness |

## Tests

`tests/test_evals_tasks.py` (8, Docker-less): task-set invariants —
unique slugs, required keys, retry declarations, single-command script
replies (bug class 1 above), arm-key/config-key sync, pre_round
completeness, pre-fix-buggy repos (bug class 3), deterministic rebuilds
(bug class 2). `tests/test_evals_run.py` and `tests/test_daily_driver_evals.py` are
Docker-less and cover selection gates before repository construction,
baseline/comparison failure, target forwarding, environment restoration,
JSON-only output, unique run roots, required/forbidden receipts, default
suite separation, combined-check isolation, active-feature coverage,
observed-comparison gates, non-vacuous ACI feedback, independent-action
enforcement, large-repo ranking, project instructions, long-session
compaction, hard-kill checkpoint restore, and blocked-lane handling.
The current three-file selection is 69 passing tests. The arms themselves
are validated by the real Docker matrix. `tests/test_tui_contract.py`
adds 4 repeated real-Textual/Pilot checks (live status, rendered diff,
worker drain, global-hook and builtin restoration); the nonstandard-order
selection of TUI + installed-wheel/private-state + daily-driver + runner
tests passed 39/39. The installed-wheel test proves console entrypoints
and package origins resolve from the wheel venv with private state; it
uses system site packages and therefore does not claim a fresh dependency
closure.

## Release reconciliation (2026-09-24, Terminal 4)

- Restored prompt-regression as the default CLI suite. Daily-driver and
  combined checks are explicit and cannot alter the standard `--check`.
- Restored the dependent `lint_names=False` companion in `no_lint` and
  made registered-arm coverage structural errors fail closed.
- Added required and forbidden trace receipts for DOCS, FETCH, lint, and
  skills so enabled features cannot silently no-op and disabled features
  cannot silently emit the guarded event.
- Verification: the 41-test Docker-less selection passed; the real
  `python -m evals.run --check` host gate passed 14/14. After the Docker
  daemon became available, the complete 14-task × 8-arm real-loop matrix
  passed 112/112 with zero regressions. A real Docker run also caught and
  pinned the former double-nested log-root path; task traces now live at
  `<run>/<arm>/<task>/trace.jsonl`, exactly where the outer scorer reads.

## Not yet implemented / deliberate scope

- The real-provider worker is implemented and required, but this checkout has
  no configured model. Its real execution is **BLOCKED**, not passed. The
  current worker proves one repository-grounded explanation canary; a broader
  real-model quality matrix across every capability remains future work and
  must not be inferred from deterministic probes.
- LSP diagnostic repair now uses the public `harness.lsp` lifecycle through
  `dd_26_lsp_diagnostic_repair`. The deterministic probe starts a real stdlib
  JSON-RPC subprocess, consumes normalized diagnostics, performs a repair, and
  verifies the cleared post-change result. It does not rename or substitute
  the existing AST lint check (`harness.lint`, `lint_gate`, `lint_failed`).
  An external production language server remains an optional deployment
  choice; the evaluator's subprocess fixture is deterministic and exercises
  the same public callable contract.
- The sampled manual-repair evidence source is not complete: verified records
  still lack explicit boolean manual-repair observations, so full readiness
  remains false even when deterministic and real infrastructure lanes pass.
- Self-critique is NOT in the prompt-regression arms: the feature has no
  real eval scenario plus ablation arm in this checkout. It is therefore
  explicitly listed in `feature_coverage.uncovered`; the runner does not
  pretend that prompt coverage proves it. This declared gap is reported
  but does not invalidate the registered matrix. When a real scenario and
  ablation land, add its key to `_ROUND_KEYS` and a matching feature arm.
- Receipt limitations: the runner checks the harness trace event
  contract, not semantic model comprehension. A matching skills event
  proves the scan receipt, while prompt-content proof remains in
  `tests/test_skills.py`; a task can still fail closed if its trace is
  missing or malformed.
- eval_fetch_webpage fetches a REAL page (pypi.org) in the arms that
  leave FETCH on: loop-machinery determinism is preserved (the fix
  never reads the content), but the fetch itself needs network. The
  runner machine already needs network for image builds, so this adds
  no new environment requirement. Content-level FETCH proofs live in
  tests/test_webfetch.py (Docker+net gated) instead.
- No per-prompt-version diffing (arms are config-key toggles; if a
  prompt TEXT changes, baseline-vs-baseline across commits is the
  comparison — run before/after and diff eval_report.json).

## LSP boundary integration (2026-09-25)

`dd_26_lsp_diagnostic_repair` now exercises the public contract rather than
checking exports. Its fixture server is a child Python process speaking
Content-Length JSON-RPC; `LspManager` performs initialize, didOpen, pull
`textDocument/diagnostic`, didChange, and shutdown. The probe passes the
normalized `Diagnostic.message` into the repair request, checks the compiler's
`diagnostics` section, repairs `app.py`, updates the open document, and checks
that the second diagnostic query is empty. `evidence.ast_lint_used` is always
`False`; AST lint remains `harness.lint` with `lint_gate`/`lint_failed`.

Verification: `python -m pytest tests/test_lsp.py tests/test_daily_driver_evals.py
-q -p no:randomly` returned **41 passed**. This is a deterministic real
JSON-RPC subprocess fixture, not a claim that an external production language
server was selected.
---

## T5 P0/W1 - the eval status vocabulary, and what the reported bug was not (2026-10-01)

**Files:** NEW `tests/test_eval_status_vocabulary.py` (27 tests).
**`evals/daily_driver.py`, `evals/run.py` and `harness/core.py` were NOT edited.**

### The report was "13 eval sites assert a bare `success`". The measured number is 0.

Reproduced before changing anything, because the honest first move with a
reported defect is to reproduce it:

| probe | result |
|---|---|
| `pytest tests/test_daily_driver_evals.py -q` | **34 passed**, 467.25 s |
| `python -m evals.run --check` | **14/14 ok, verdict CLEAN** |
| `dd.run_feature_evidence(root)` | **28 pass / 0 fail**; all 20 observed `core_run_succeeded` assertions genuinely `True` |

So **no eval site was failing**. The report named a real code SHAPE and a
non-existent failure count, and the two must not be conflated: the shape is worth
pinning, the count was wrong.

### What the shape actually is, and why it is not a bug in most places

There are two live vocabularies in the tree and they are not the same set:

* `shared/types.py::TaskResult.status` is `success | failed | error | timeout`.
* `shared/agent_contracts.py::RUN_STATUSES` is `blocked, cancelled,
  completed_unverified, completed_verified, failed, needs_input, timeout` -
  **no `success`**.
* `harness/core.py:1944` is the ONLY place `completed_verified` becomes
  `TaskResult.status == "success"`, and it is gated on the verifier.

So a bare `status == "success"` comparison is correct for a `TaskResult` and
wrong for a `RunResult`. `evals/daily_driver.py` uses the former (and already has
`_honest_completion` for the latter, used at 4 call sites). Grep finds 12 literal
`status == "success"` comparisons in that file at lines 1133, 1143, 3864, 3922,
3986, 4050, 4247, 4316, 4379, 4454, 4530, 6731 - **all 12 are honest**, and the
feature lane's 28/28 is the evidence rather than an argument.

`cli.runview.run_verdict` is the shared fail-closed reduction over both sets.
It was **not** adopted at these sites, because there was nothing failing to
convert and rewriting 12 working assertions to route through a shared helper is
a refactor, not a fix. If a future report claims these sites are broken, that is
the claim to re-measure first.

### What was added instead of a "fix"

`tests/test_eval_status_vocabulary.py` pins BOTH directions, which is the part
that matters:

* kernel-owned status surfaces (`run_agent`, `agent_kernel`, `RunResult`) may not
  assert a bare `"success"` - the anti-rot gate for the historical shape, built
  from an AST scan of `evals/**`;
* the legitimate `TaskResult` / `run_question` producers are EXEMPTED by
  construction (derived from the parsed tree, not a hand-kept name list), so the
  gate cannot be satisfied by deleting the correct assertions;
* the verifier-gated `COMPLETED_VERIFIED -> success` mint is pinned as the only
  one;
* `_honest_completion` semantics are pinned;
* `cli/ui.py::sanitize_text` strip-before-redact and fail-closed display are
  pinned (T4's landed fix), so the redaction ordering cannot silently regress.

**Verification:** 27 passed in 3.04 s; `ruff check` clean.

### Not yet implemented - T5 W1.3, the Trust Ladder

**This is deliberately NOT built, and the reason is a missing specification
rather than a hard task.**

The contract I hold is: 10 rungs; rung statuses exactly
`pass | fail | blocked | not_implemented`; **no `skip`**; machine-readable JSON
plus a human table; wired as `python -m evals.run --suite trust-ladder`; absent
data reported as `available: false` rather than as a pass. **The identities of
the 10 rungs are not recorded anywhere in this tree** - not in `project-spec.md`,
not in `INTERFACES.md`, not in `AGENTS.md`, not in any `logs/*/` handoff, and not
in any eval module.

Given that, the choice was between inventing 10 rungs and leaving the lane
unbuilt. Inventing them would have produced a ladder that renders a clean
10/10 table and reads like a specification, which is the specific dishonesty
this repo's doctrine exists to prevent: **a metric that looks authoritative and
measures nothing the spec asked for.** The user was asked and chose to skip T3
and spend the effort on W1.4. The contract above is recorded here so the next
terminal does not have to re-derive it; supplying the 10 rung identities is the
only missing input.

Nothing in `evals/run.py` was touched, so `--suite trust-ladder` does not exist
and no code path claims it does.

## T5 P1/W1 - the Trust Ladder exists, and rungs #8/#9/#10 are executable (2026-10-02)

**Files:** NEW `evals/trust_ladder.py` (the scorecard), NEW
`evals/trust_ladder_rungs.py` (the three real probes), NEW
`tests/test_trust_ladder.py` (43 tests), NEW
`tests/test_code_graph_caller_index.py` (9 tests), NEW `evals/ci_lanes.py`
+ `.github/workflows/ci-lanes.yml` + `tests/test_ci_lanes.py` (21 tests),
NEW `tests/test_provenance_binaries.py` (48 tests), NEW `NOTICE`, NEW
`provenance/binaries.json`, NEW `docs/phase1-daily-usable.md`.
`evals/run.py` gained the `trust-ladder` suite. `memory/code_graph.py`,
`scripts/provenance_report.py` and three workflows were edited.

### The brief said "last wave you built the Trust Ladder". It was not built.

`evals/AGENTS.md` records the decision honestly: T5.P0/W1.3 **deliberately did
not build it** rather than invent ten rungs, and closed the P0 handoff with
"supplying the 10 rung identities is the only missing input". The identities
were in `phases/P0-foundation/W1/T5-platform.md:131-142`. They are now in
`evals/trust_ladder.py`, and the suite is registered.

### Read this before adding a rung

**One authority per concern.** `Rung8Result.derive_failures()`,
`Rung9Result.derive_failures()` and `Rung10Result.derive_failures()` are the
ONLY definitions of "does this rung fail". The probes call them and the test
fixtures call them.

> This is not a style preference. The first version had the rule inline in
> `probe_rung8` and the fixtures setting `failures=[]`, and **four of the five
> demonstrated rung-#8 breaks passed green**. A test that fabricates a
> measurement must not also fabricate the verdict.

### `Timing` enforces the percentile rule in the type

`MIN_SAMPLES_FOR_P95 = 20`. Below it, `named_stat` returns `"median"`,
`headline` is the median, and `to_dict()` sets `percentiles_withheld` and
**omits the `p95` key entirely**. An unmeasurable metric sets
`available: false`, sets `reported_as: "unavailable"`, and carries **no
`value` key** — so a consumer that reads `value` without reading `available`
gets a `KeyError`, which is the right outcome for a number nobody took.

### Rung #9's diagnosis is MEASURED, never inferred

`attribute_retrieval()` profiles the call and names a `dominant_phase` only
when one phase exceeds `ATTRIBUTION_DOMINANT_SHARE = 0.50`, and always reports
`graph_rebuilt` so a warm-run attribution cannot be quoted as a cold one.

> The first version reported "the skip list does not cover this repository"
> from a >10x ratio, and **that was a lie**: T1 landed a 45-entry shared skip
> set while the probe was being written, the walk went 147,239 -> 157
> directories, and the ~50 s that remained had nothing to do with the walk.
> A ratio against a control is a diagnosis only when the control isolates the
> bottleneck. If it does not, say "no cause" - the rung supports that.

### Two real defects this module's own tests found in itself

1. **A `pass` with two of four metrics `unavailable`.** Rung #9 is a
   conjunction; a conjunct nobody measured has not been shown to hold. Now
   `blocked` with the reason, pinned by
   `test_rung9_cannot_pass_with_an_unavailable_metric`.
2. **A false pass on the cap-receipt check.** It substring-searched every
   journal row for `max_turns` and came back green on the `task_start` row,
   whose `data.config` echoes the whole merged config. That is the run stating
   its configuration at turn 0, not a cap being **approaching**. The check now
   requires a non-terminal row carrying BOTH a cap and a
   remaining/approaching field.

### `evals/ci_lanes.py` - the six lanes are data

`python -m evals.ci_lanes` exits 2 when the workflows disagree with the
authority: a missing file, a missing job, a runner mismatch, a
`timeout-minutes` over budget, a `docker` invocation in a Docker-free lane, or
a lane with no registry gate. It found four real disagreements on its first
run, including a `nightly` lane pointing at a job that did not exist.

**The one rule:** `REGISTRY_COMMAND` runs before the first test invocation in
**all six** lanes. Not "the lanes that run the pins" - every lane. A registry
some lanes read and others ignore is a registry whose promoted pins are
promoted in one job and invisible in five.

**The two budgets are ceilings, not targets.** A workflow may only be slower
than its budget if the budget was raised in `evals/ci_lanes.BUDGETS` first, so
the change shows up in a diff on the authority rather than as a mysteriously
slow lane.

### The binary provenance shape

A binary is declared in `provenance/binaries.json` because **a binary has no
header**. `sha256` is re-hashed from disk every run; a mismatch is
`mismatched`, kept separate from `incomplete` because it is the supply-chain
case. `fetched_binaries` is a second section for run-time downloads and does
**not** require `sha256` - there are no bytes to re-hash, and requiring a
digest nobody can verify is the paperwork version of the same dishonesty.

`licence: "MIT or Apache-2.0"` is **rejected**: a dual-licensed dependency with
an unstated choice is an unanswerable licence question. ripgrep is **MIT OR
Unlicense** (not "MIT", which is what the P1 brief said), fd is **MIT OR
Apache-2.0**; MIT is relied on for both and `NOTICE` says why.

### Not yet implemented

- Rungs #1-#6 are **carried**, not re-measured. In a checkout without
  `logs/gates/g0_report.json` they are `blocked` with a runnable remedy.
- Rung #7 is `not_implemented` (no gate measures cross-session recall).
- The binary lane cannot see a binary another module fetches at run time; the
  `fetched_binaries` section is a declaration the project makes about itself.
- No live-provider lane ran. **Every model call in every rung is a scripted
  double, so no number in `docs/phase1-daily-usable.md` is a claim about
  model quality.**

## P3a/W1 (2026-10-03) - the memory ablation arm and the G3a gate

**Files:** NEW `evals/memory_ablation.py`, NEW `evals/gates/P3.py`, NEW
`tests/test_p3a_memory_ablation.py` (49). **`evals/run.py` was NOT edited** and
neither was `evals/tasks.py`. The full contract is `INTERFACES.md`'s "P3a Memory
Contract".

### WHY this is not an entry in `evals.run.ARMS` - read before adding one

`tests/test_evals_tasks.py::test_runner_arms_use_real_config_keys` requires
**every key in every `ARMS` entry to exist in `harness.config.DEFAULTS`**, and
`DEFAULTS` lives in `harness/**`, which T5 may not edit. The phase also forbids
adding a truthy `DEFAULTS` key, because a value there is merged into every task
and every eval arm.

So the memory arm is driven by **KEY PRESENCE** (`memory_recall_enabled`,
`memory_pipeline_enabled`, `memory_ranking_enabled`), read by presence AND
explicit truthiness, and pinned absent from `DEFAULTS` by
`tests/test_p3a_memory_ablation.py::TestConfigDiscipline::test_no_memory_arm_key_is_in_harness_defaults`.
**Do not migrate these into `ARMS`** -- it will fail that pin, and the pin is the
mechanism.

### Every arm declares its FULL key set, and that is load-bearing

An arm that set only the key it wanted to change would, under key-presence
config, silently switch every other feature off too. The first draft of
`MEMORY_ARMS` did exactly that: `no_ranking` set only
`memory_ranking_enabled=False`, so it ALSO switched recall off and the suite
reported `recall@k: not measured` for the arm whose entire purpose is to isolate
ranking. `evals/gates/P3.py::rung_r2_ablation_arms` is what caught it.

`pre_phase` is the only arm exempt from the one-axis rule (switching off
everything is its purpose, exactly as `pre_round` is in `run.py`), and the gate
checks it turns off EVERY key rather than a partial subset.

### Recall is scored in `match_mode="or"`, and the report says why

An AND search over a labelled query set whose second term is a paraphrase
measures the labelling, not the retrieval -- the first run reported
`recall@k = 0.000` for a corpus that plainly answers every query. The mode and
its reason are published in the report (`match_mode`, `match_mode_reason`) so
the number cannot be quoted without the setting that produced it.

### `recall@k` measures retrieval, NOT task success

The arm's `claims` list says so on every run and the report is built so a reader
cannot read the first number as the second. The citation set is **injected by
the arm**, not observed from a run, because there is no loop here to observe it
in. **The AHE +5.6pp figure in the phase brief is the corpus's measurement of a
different system**; nothing here reproduces it.

### The G3a gate is RED, deliberately

19 pass / 0 fail / **1 blocked** / **1 not_implemented**. This gate counts a
blocking `blocked` as RED, unlike G0/G1: a lane that could not run is precisely
the lane that would have falsified a retrieval claim.

`_selftest()` is the "a gate must be able to fail" proof: it mutates the gate's
own derivation and asserts the verdict moves, that `"skip"` cannot be
constructed, and that a non-blocking `fail` does NOT redden the gate.
`python -m evals.gates.P3 --prove` additionally runs the named proving test
FILES (not a `-k` filter -- an earlier G2 draft filtered on a substring that
matched neither of its own pin files and reported a closed gap as `fail`).

### The gate found five real defects on its first run

All five were in the code it was gating, not in the gate. Two are worth not
re-introducing: an **inverted condition in the revocation probe** (introduced
while fixing a `TypeError` on the same line, caught by re-running rather than by
reasoning), and a probe whose **clock step was smaller than the backoff it was
measuring**, which reported a queue with no terminal state that did have one.

### Not yet implemented

- **`r12_docker_lane` is `not_implemented`, owned by P3a-W2.** No Docker-backed
  memory measurement exists yet.
- **No live-provider lane ran.** `r11` is `blocked` with that exact reason.
- **Rung #7 of the Trust Ladder** ("understands the project on day 2") is still
  `not_implemented` in `evals/trust_ladder_rungs.py`. `evals/memory_ablation.py`
  is now the measurement that rung was waiting for; whoever wires it should
  point the rung at this module rather than writing a second one.

## P3b/W1-T5 (2026-10-05) - the tools ablation, and the prompt gate

**New:** `evals/tools_ablation.py` (926 lines), `evals/prompt_pin.py` (720),
`extensions/skill_registry.py` (957), `tests/test_p3b_tools_and_prompts.py`
(999, 88 tests, ~1 s). **Edited:** `evals/ci_lanes.py` (+20 lines of mine; the
file is shared and dirty, so `git diff --numstat` over-reports),
`.github/workflows/ci-lanes.yml` (+25 lines of mine, same caveat).

```powershell
python -m evals.tools_ablation            # the tools ablation
python -m evals.tools_ablation --vacuity  # re-derive the vacuity evidence
python -m evals.prompt_pin                # exits 2 on an unarmed prompt edit
python -m evals.prompt_pin --update       # PRINTS digests; never writes
```

### The tools ablation, and the vacuity question that decided it

The brief asked whether a deferred-loading arm under the mock provider actually
differs. **Answered mechanically before anything was built**, and the answer is
split:

* **The request axis is REAL.** The schema list is built per turn by
  `strategy._schemas_for_model` and threaded to the Boundary-2 call, so
  shrinking it shrinks the bytes.
* **The success axis is VACUOUS under the shipped mock.**
  `runtime/mock_provider.py` and `evals/run.py` contain **zero** occurrences of
  the token `tools`, `synthesize` is `(model, messages)`, and the scripted
  responder's signature is `fn(messages, model)` with **no `tools` parameter**.
  The schema list is dropped at the model boundary, so a task outcome cannot
  move. `verify_vacuity()` re-derives all of this from source **on every run**
  and reports `agrees_with_record`, so a future change to the mock that made
  the axis non-vacuous is DETECTED rather than silently obsoleting the claim.
* **The provider-reported token axis is also vacuous here** - every scripted
  call reports a fixed token count, the same defect `docs/p3a-g3a.md` records
  for the memory ablation.

So the driver uses **`SchemaAwareModel`**: a *scripted-deterministic* model
whose signature NAMES `tools` and whose behaviour genuinely depends on the
schemas it is given. No socket, no credential, bit-identical run to run - but
unlike the shipped mock it can be HURT by deferral. No live provider ran.

**Measured, on the real 47-tool harness catalog:**

| query | eager schemas / tokens | deferred | delta |
|---|---|---|---|
| matches the needed tool | 47 / 3631 | 42 / 3236 | **-395 (-10.9%)** |
| does NOT match it | 46 / 3591 | 29 / 2141 | **-1450 (-40.4%)** |

**And the number the headline hides:** the second row costs
`extra_round_trips_for_discovery = 1`. Deferral buys prompt tokens with an
EXTRA MODEL ROUND TRIP, which is free on a scripted double and is **not** free
on a real provider. Both numbers are reported together, deliberately, so a
token win cannot be read as a latency win.

`discoverability_failure_rate` is **0.0 in both arms** - and that is structural
rather than lucky: with a `select:` form, any tool in the catalog resolves
exactly. The real regression risk is **"the model never thinks to look"**, which
a scripted model cannot exhibit. Stated rather than papered over.

`cost_usd` is the string **`"unpriced"`**, never `0.0`, with `cost_priced:
False` (`DOCTRINE.md` section 1: a `$0.00` reads as cheapest to a price
ladder). `verdict` is `MEASURED` - never `CLEAN` for a zero delta.

`single_variable_report` returns `IDENTICAL_ARMS` for two equal arms, which is
**the vacuity trap this phase's brief names**, and `attributable: False` for it.

### The prompt gate: "prompts land as eval arms, never as edits"

AHEAD measured this repository's own agent system prompt at **-2.3pp for prose
alone**. `evals/prompt_pin.py` makes that a gate rather than a convention.

`AGENT_FACING` is 9 constants. Each is AST-parsed and its string **VALUE** is
digested (normalised for line endings, or every Windows checkout reports every
prompt as edited). A reformat or a docstring edit is therefore not a prompt
change; a one-character prompt edit is.

**`CONTROL_ONLY_ARMS = ("baseline", "pre_round")` is load-bearing, and it exists
because the gate was DEMONSTRATED to be vacuous before it was added.** Every
coverage entry initially included `baseline`; `baseline` exists; so every edit to
every prompt read as "armed" and the gate passed a real unarmed edit. The
reasoning: an ablation arm's purpose is to DIFFER from baseline. A prompt
exercised only by `baseline` is exercised IDENTICALLY by every arm, so no
comparison can see a change to it. Such a prompt has no arm that would detect
the change - which is exactly what the rule exists to prevent.

**Seven of the nine agent-facing constants are `coverage_gaps`** (covered only
by control arms) and are published on every run as the work list. The gate is
GREEN today and goes red the moment one of them is edited. That is a real
coverage hole in the matrix and it is on the record rather than hidden.

Bypass resistance, each against a way a pin in this repo has been defeated:

| defence | against |
|---|---|
| exemptions keyed by CONSTANT NAME; a glob is refused by `_wildcard_exemption_findings` | a blanket suppression |
| every exemption needs a non-empty `reason` + `owner`; a changed digest under one is a `stale_exemption` FINDING | an exemption nobody re-reviewed |
| `MIN_PINNED_CONSTANTS` fails an emptied baseline | deleting the pins to go green |
| a constant nobody classified is an `unclassified_prompt` finding | adding a prompt with no arm |
| no env var, no `skip=`, no write path; `--update` PRINTS | a gate that rewrites its own baseline |

Registered in the **`smoke`** lane (blocking, `<30 s`, one AST parse, no Docker,
no network, no test collection) plus the P3b pins.

### The seam for T1 - landing a prompt change correctly the FIRST time

1. Edit the constant in `harness/prompts.py`.
2. Register an arm that exercises the change and list the constant under it in
   `evals/prompt_pin.ARM_COVERAGE`. For a prompt-adjacent config key that is an
   entry in `evals.run.ARMS` (auto-discovered by `registered_arms()`); for an
   arm this driver owns, add it to `evals/prompt_pin._EXTRA_ARMS`.
3. `python -m evals.prompt_pin --update` and paste the digests.

Skipping step 2 exits 2. Three commands, and the gate is happy on the third.

### The five-row prompt-surface audit

| row | exists | an arm? | enforced or aspirational |
|---|---|---|---|
| anti-gold-plating | yes, 3 qualitative sentences (`prompts.py:186`, `:787`, `:238`) | **no** | **ASPIRATIONAL** |
| verbosity directive | **no** quantified one; qualitative only | **no** | input side only |
| precedence contract | **DOES NOT EXIST** | no | the untrusted FILTER is enforced; the contract is not |
| read-before-edit | prose is weak and not in `prompts.py` | **no** (a measured A/B in a config comment) | **STRUCTURAL** |
| verify-before-claim | yes (`prompts.py:191`, `:788`, `:912`) | **no** | **STRUCTURAL** - strongest of the five |

Nothing was added as prose. Row 1 is aspirational and **should stay that way
until an enforcement point exists** - adding a fifth sentence about minimality is
the measured failure mode this phase exists to prevent. Rows 4 and 5 are
already structural, so no prose was added for them either.

## T5 P3a/W2 (2026-10-04) - the real loop ablation, the inbox gate, and the measurement that says memory does not help yet

**Created:** `evals/memory_ablation_real.py`, `evals/memory_quality.py`,
`evals/inbox_gate.py`, `tests/test_p3a_inbox_gate.py` (21),
`tests/test_p3a_memory_quality.py` (29), `tests/test_p3a_security_repins.py` (14),
`docs/p3a-g3a.md`, `docs/p3b-brief.md`.
**Edited:** `memory/inbox.py`, `shared/security.py` (additive receipt only),
`evals/run.py` (one line), `evals/gates/P3.py`, `evals/trust_ladder.py`,
`evals/ci_lanes.py`, `.github/workflows/ci-lanes.yml`, `INTERFACES.md`.
**NOT edited:** `harness/`, `execution/`, `runtime/`, `cli/`, `mcp_server/`.

### Run it

```powershell
python -m evals.memory_ablation_real --repeats 3   # 168 Docker-verified runs, ~25 min
python -m evals.memory_quality                     # precision / staleness / retraction / harm
python -m evals.inbox_gate                         # the 5-point proof
python -m evals.gates.P3                           # G3a, exit 2 when red
python -m evals.run --suite trust-ladder           # rung #7 is now measured, and red
```

### The result: a NULL, with a cost attached

Oblivious lane (the shipped scripted double, which cannot read memory):
`1.0000` vs `1.0000`, delta `0.0000` — a null **by construction**.

Grounded lane (a declared memory-reading policy): `1.0000` off, `0.928571` on,
delta `-0.071429`, **intervals overlap, `attributable: false`**.

**And then the number that outranks the delta:** of 42 matched pairs, **3
outcomes moved and 3 of 3 moved FOR THE WORSE** — all `bug01_wrap`, all three
repetitions, all caused by the stale `FILE-ALREADY-FIXED` memory. The grounded
agent read "already fixed in an earlier session", skipped the edit, and failed
verification. The oblivious agent never read it and solved the task.

**Memory cost nothing, helped nothing, and hurt when it was read.** The corpus
report says why: staleness `0.166667` (1 of 6 recalled memories contradicts the
repository bytes — that one), precision `0.666667` (4/6), retraction detection
`1.0` (5/5) with **`amended_count: 0`**.

### `amended_count: 0` is the most important number in this wave

Detection works. **Nothing reads the flag.** A memory the snapshot knows is stale
still reaches the planner and is still acted upon — which is the bug01 failure,
in full. `docs/p3b-brief.md` makes "make a detected-stale memory stop reaching the
model" item #1, with the success criterion `outcomes_moved_for_the_worse: 3 -> 0`.

### Three defects these measurements found in the code they measured

1. **A rejection was not sticky.** `ProposedMemory.digest` includes
   `session_id`, so the same sentence extracted by the NEXT session hashed
   differently and a human's "no" expired at the end of the session — the gate
   was decorative in exactly the way it existed to prevent. Fixed with
   `content_digest` (excludes `session_id` and `category`, keeps `source`).
   Pinned by `test_a_rejection_survives_the_next_extraction_run`.
2. **`unverifiable` was counted as `false`.** `check_claim` reports a missing
   repository as severity `high` — correctly, since not being able to check a
   memory is serious — and the first classifier folded severity into the verdict,
   publishing **9 stale memories out of 9 repositories the report had just
   deleted**. KIND is the discriminator. Then a second false negative: the
   fixed-file check ran the whole suite, so a bug in `numlib/mathutil.py` refuted
   a true claim about `numlib/__init__.py`. An unattributable failure is now
   `unverifiable`, never `false`.
3. **`ast.walk` is breadth-first.** The inbox gate's "parents" list was not an
   in-scope chain, so every call site was attributed to `<module>` — and it found
   ZERO writes through the inbox, because `MemoryInbox.apply` reaches the store
   via `store.record`, not through a call to `.apply(`. That reads as "nothing is
   gated" rather than "the gate is here".

Also fixed, smaller: `propose()`'s `suppressed_by_rejection` was publishing the
size of the known-digest SET (a 1-item proposal reported "2 suppressed"). Now two
keys — `suppressed_by_rejection` (items) and `rejected_digests_known` (digests).

### `shared/security.py`: the receipt, not the disposition

The credential-in-memory canary found that a memory row holding a live credential
was stored correctly redacted and then reported `reason=""`, `findings=()`,
`severity="none"` — indistinguishable downstream from a row that never held one.
The first fix appended a **finding**, which BROKE a pinned contract: in `block`
mode any finding refuses the write, so credential-bearing memories started being
quarantined instead of stored-redacted, and `test_ceiling_security.py` caught it.

The notice now travels in `reason` as `MEMORY_CREDENTIAL_REDACTED_REASON`. It is
a note about what was removed, not a policy violation, and
`test_the_redaction_notice_is_not_a_policy_finding` pins both halves so nobody
"fixes" it by adding the category to `_MEMORY_AUTHORITY_CATEGORIES`.

### The DECLARED bypass — do not close this by deleting it

`DecisionStore.ingest_state_file`, driven automatically by `poll()`/`watch()`,
promotes Boundary-4 state-file rows with **no human in the loop**. It predates the
inbox and is a deliberate contract. `evals/inbox_gate.DECLARED_BYPASSES` names it
with a reason, `r13_inbox_gate` publishes it, and
`test_the_state_file_bypass_is_declared_not_hidden` fails both if the path is
silently routed through the inbox and if the declaration is deleted. **The trust
claim covers the EXTRACTION pipeline, not every automated write.**

### `evals/run.py` — the one shared-surface edit, and why

`no_memory` bound `db_path` only when the arm was NOT memory-off, so the OFF arm
ran with **no store at all**. That is "memory off AND no seed", and any comparison
against it is unattributable. Both arms now receive the same seeded store and
differ only in `plan_with_memory`. This changes an existing arm's behaviour and
is called out in `INTERFACES.md` for that reason.

### Verification actually run

| command | result |
|---|---|
| `pytest tests/test_p3a_{inbox_gate,memory_quality,security_repins,inbox,governance,memory_ablation,snapshot,search_index,pipeline}.py -q` | **346 passed, 1 skipped** (227 s) |
| `pytest tests/test_decision_store.py test_tracing.py test_config_trace_state.py test_mcp_server.py test_mcp_adversarial.py test_security_regressions.py test_ceiling_security.py test_trust_ladder.py test_ci_lanes.py -q` | **247 passed, 5 skipped, 6 failed** — see below |
| `pytest tests/test_trust_ladder.py -q` | **39 passed, 4 failed** (after fixing my own regression) |
| `python -m evals.inbox_gate` | **INBOX_GATE_GREEN**, 5 pass / 0 fail |
| `python -m evals.gates.P3` | **G3A_RED**, 14 pass / 0 fail / **1 blocked** (live provider) |
| `python -m evals.run --check` | 14/14 CLEAN |
| `python -m evals.ci_lanes` | CI_LANES_CONSISTENT |
| `ruff check` | clean on every owned file |
| `docker info` | Server 28.5.1, overlayfs |

**I caused one of those 6 failures and fixed it.** My first rung-#7 wiring called
the probe from inside `carried_rows`, so `ladder_report(run_measured=False)` still
listed rung 7 as MEASURED and ran a multi-minute quality report in the mode whose
whole contract is "run nothing" — the exact false claim
`test_unrun_probes_are_not_listed_as_measured` exists to prevent. `carried_rows`
now takes `run_measured` and rung 7 falls back to `not_implemented`.

### The 4 remaining `test_trust_ladder.py` failures are NOT mine — evidence

* `evals/trust_ladder_rungs.py` is **modified in the working tree by another
  terminal** (`git diff --stat`: 103 insertions, 33 deletions) while
  `tests/test_trust_ladder.py` is **unmodified from HEAD**. So
  `test_rung9_reports_fail_when_a_budget_is_exceeded` (asserts `"code_graph_build"`
  in the detail; the shipped rung 9 now reports phase attribution INCONCLUSIVE)
  is a stale test against someone else's unlanded change.
* `logs/gates/g0_report.json` exists as an **untracked local artifact**, and
  rungs 1/3 are CARRIED from it. Its rung-1 row says `pass` (Docker reachable) and
  its rung-3 row says the pin registry exited 1 — while `python -m
  tests.known_failing_pins` exits **0** with 0 build failures right now. So
  `test_a_carry_without_a_published_report_is_blocked_never_pass` and
  `test_a_blocked_row_alone_does_not_make_the_verdict_red` are asserting against a
  stale report, and `test_the_report_is_json_serialisable...` sees `MEASURED_RED`
  because of it plus my honestly-failing rung 7.

**I did not edit `tests/test_trust_ladder.py` to make these green.** Editing
another terminal's test to clear their red is how a red gets laundered instead of
fixed. Re-run `python -m evals.gates.P0` to refresh the report and the two
`blocked` fixtures resolve; the rung-9 test needs whoever changed the rungs to
retarget it.

### Not yet implemented

- **Nothing consumes a staleness flag.** Detection without consumption is half a
  control. This is item #1 of `docs/p3b-brief.md`.
- **No live-provider lane ran**, so nothing here is a model-quality claim.
- **The token/cost axis is vacuous** — every scripted call reports exactly 20
  tokens, so the estimator cannot see prompt length. Published as
  `cost_axis_vacuous: true` rather than quoted as `$0.00`.
- **9 of 14 tasks recalled nothing at all.** Counted, not scored. Whether the
  corpus does not cover them or retrieval does not reach it is **not measured**.
- `evals.memory_quality` and `evals.inbox_gate` are not registered in
  `evals/run.py`'s suite table. Wiring them is a decision, not an oversight.
- **9 of 10 files in `logs/evals/` are gitignored**, so the reports a reviewer
  needs are not in the tree. `evidence/bundles/` is the committed counterpart and
  no bundle covering this wave exists.

## P4/W1 (2026-10-06) — the multi-session evaluation, and what it does not measure

**New:** `evals/multi_session.py` (~1330 lines), `tests/test_multi_session_eval.py`
(36 tests, ~39 s), `tests/test_p4_long_horizon_gaps.py` (12 tests, ~2 s).
**Edited:** `evals/run.py` (suite registration + `_run_multi_session`),
`evals/ci_lanes.py` (lane placement), `tests/test_agent_loop_matrix.py` (one dead
import). **`harness/`, `execution/`, `runtime/`, `cli/` were not edited.**

```powershell
python -m evals.run --suite multi-session --arms initializer --repetitions 3
python -m evals.multi_session --json
python -m evals.run --suite multi-session --repetitions 0     # usage error, exit 2
python -m evals.run --suite multi-session --tasks foo        # usage error, exit 2
```

### The docstring overstated the runner, and a pin now holds it to the truth

The first version of this module's docstring claimed *"a session here is a fresh
`harness.agent_loop.run_agent` call with a fresh conversation"* whose *"first
model request is built ONLY from durable artefacts"*. Half of that was true and
half was not, and the untrue half was the part that would have made a reader
trust the numbers.

What is real: `build_session_brief` reads only `init.sh`, `features.json`,
`PROGRESS.md`, the request and the constraints, holds no message list, and is
proven to carry nothing across sessions by a planted marker — **the
survival-vs-compaction distinction is genuinely under test.**

What is not: the session body is `_scripted_session`, which writes the feature's
implementation directly and verifies it through the injected gate. It does **not**
call `run_agent`, and `session_turns` is recorded on the report while nothing
reads it. The task is multi-session because the *runner assigns one feature per
call*, not because a session ran out of turns.

`TestTheModuleDoesNotClaimMoreThanItDoes` asserts both directions: the module may
not advertise a real agent session it does not make, and it may not quietly drop
the specific limit sentence if it starts making one. A one-way check would be
satisfiable by deleting the docstring.

### The consequence is published, not hidden

`discrimination()` reports `metrics_that_vary == ["parent_context_tokens",
"tokens_per_session"]` — the initializer's brief is genuinely longer, so it
genuinely costs more parent context. **None of the three outcome metrics
(`completion`, `rework_rate`, `constraint_retention`) move**, and those are the
ones the four arms exist to move. A pin asserts that exact split, so the day an
outcome metric separates it fails loudly instead of being quietly reported as a
result. The `read_before_edit` and `subagent_isolation` arms set mechanism keys
the scripted body never exercises, so they are structurally incapable of
separating today.

### `null` is a value; `0` is a measurement

`cost_usd` is `null` with a reason on every arm, and the reason says a `$0.00`
would be a fabrication. `parent_context_tokens` and `tokens_per_session` are
**heuristic** (`chars/4`), labelled as such in the render. `MODEL_QUALITY_
MEASURED` is `False` on every receipt. The exit code is 2 only when **no arm
produced any measurement** — never because a metric was null, because a gate
that punishes honesty teaches the next person to fill the null in.

### `smoke` got nothing, and that is a measured decision

Phase 4 asked for three promotions into the blocking `smoke` lane. Two meet the
lane's own criteria (structural pins ~1.2 s of pure AST; gap pins ~1.6 s) and
the third is the 24.6 s pure-vs-adapter matrix T3's refactor needs. **`smoke` is
already over its budget** — 96.3 s recorded against `<= 1` minute — and
re-measuring on this tree did not finish inside fifteen minutes with four
parallel terminals running. That figure is contaminated by host load and is
quoted as a lower bound, not as a clean measurement; it is still not evidence of
room. Widening a lane that is already over is a decision for whoever owns the
number, so all four files went to `host-only` instead. Both lane notes say so,
and a pin asserts the promotions are *withheld* for that stated reason.

### One gap pin was re-stated rather than kept

T3 landed `runtime/turn_middleware.py` + `runtime/turn_policies.py` **during this
phase**. `SEMANTIC_ORDERINGS` is now declared with a written reason per rule and
`ordering_violations` enforces it at composition time, so my original pin ("no
pipeline exists, so the ordering is unjustified") closed on arrival — which is
what it was written to do.

The pin was rewritten to the half still open: **nothing drives the pipeline.** No
`runtime/test_*middleware*.py` exists and no test anywhere imports
`TurnPolicyPipeline`, while the module's own comment claims each rule "is asserted
behaviourally ... not by comparing indices". The declaration is checked against
itself; nothing checks the pipeline against the declaration. The scan matches an
**import statement**, not a bare mention, because this file and the structural-pin
file both name the class while asserting it is unused — a mention-matching scan
would report the pin closed the moment it was written. Owner remains T3.

### Not yet implemented

- **The session body is not the real loop.** This is the module's largest gap and
  it is the reason the arms cannot separate. The successor drives real bounded
  `run_agent` sessions with a real turn ceiling; nothing in the current code
  pretends otherwise.
- **One task, one shape.** `TASK_SLUGS == ["multi_session_shop"]`, pinned as gap
  #5. A single task means the metric means one thing and nothing else.
- **No live provider ran.** Nothing here is a model-quality claim, and
  `MODEL_QUALITY_MEASURED` says so on every report.
- **Token metrics are a `chars/4` heuristic**, not a tokenizer. Real per-session
  token counts need a live provider.
