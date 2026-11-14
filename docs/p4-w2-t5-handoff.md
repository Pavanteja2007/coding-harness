# T5.W2 — Handoff

**Terminal:** T5 · **Phase:** 4 (Long-Horizon Autonomy), Wave 2 · **Date:** 2026-10-06
**Exit gate:** G4 — a 3+ session task completes correctly, measured. → **`pass`** (`docs/p4-g4.md`)
**Scope owned:** everything except `harness/**` `execution/**` `runtime/**` `cli/`
**Sole `INTERFACES.md` writer:** no contract change was made this wave, so no Change Log entry is owed.

---

## 1. Files changed, with real line numbers

### New

| file | lines | what |
|---|---|---|
| `tests/test_p4_drift_audit.py` | 555 | the drift pins — 12 tests. The non-vacuity gate is the point: it asserts the control probe and every drift probe end in DIFFERENT observable states. |
| `scripts/p4_drift_audit.py` | 434 | `python scripts/p4_drift_audit.py` — 4 drift probes + the audit-trail transcript. Exit 2 if any probe is `HANDLED_BY_NOTHING`. |
| `scripts/p4_test_adjudication.py` | 512 | `python scripts/p4_test_adjudication.py` — the four watch-lists as 8 mechanical checks. |

### Rewritten

| file | change |
|---|---|
| `evals/multi_session.py` | 1330 → **2355** lines. Session body replaced (`_scripted_session` → `_bounded_session`); arm-realness verifier; `control_arm_turn_arithmetic`; `spread()`/`exceeds_own_noise`; `isolation_summary`; second task shape; `DEFAULT_SESSIONS` 3 → **4**, `DEFAULT_SESSION_TURNS` 6 → **3**. Two correctness fixes at the end of the wave (§8.1, §8.2). |
| `tests/test_p4_long_horizon_gaps.py` | gaps 4 and 5 **closed and their pins deleted**; a coverage-honesty replacement pin added; 3 gap pins retained; plus the post-session dependency-chain pin that makes gap 5's closure real. |
| `tests/known_failing_pins.py` | 2 `InvertedPin` entries deleted; 3 re-recorded with fresh measurements + a "will this close?" assessment each. |
| `tests/test_multi_session_eval.py` | 36 → **40** tests. Stale W1 contract tests rewritten; two new pins on the variance receipt. |

### Edited

| file:line | change |
|---|---|
| `memory/feature_ledger_store.py:1387` (`apply_status`) | **the drift fix**: `allow_cross_session_evidence=False`; new `evidence_session_mismatch` refusal after `_require_passing_evidence`. |
| `memory/feature_ledger_store.py:1594` | history rows gain `evidence_session` and `cross_session_evidence`. |
| `memory/feature_ledger_store.py:202` | `evidence_session_mismatch` added to `STORE_REFUSALS`. |
| `evals/ci_lanes.py:306` | `tests/test_p4_drift_audit.py` + `runtime/test_p4w2_middleware_proof.py` registered in `host-only`. |
| `evals/session_journey.py:775` | the `EVENT_VOCABULARY` gap re-stated at its measured size (**17 kinds**) + `unmapped_feed_kinds()`. |
| `evals/session_journey.py:1962` | the rail's frame check moved from `findings` (which the diff compares) to `metrics`. |

### Docs

`docs/p4-g4.md` (G4), `docs/p5-brief.md` (Phase 5 brief).

---

## 2. Raw output — all ten commands

### 2.1 `python -m pytest tests/ -q`

```
(no output — the command tool terminated it after 5400 s / 90 min with no output)
```

**REPORTED AS NOT RUN IN ONE PROCESS, not as a pass and not as a fail.** This
matches what `runtime/AGENTS.md` and `evals/AGENTS.md` already record for this
tree. Covered in chunks instead:

```
CHUNK A  tests/test_multi_session_eval.py tests/test_p4_long_horizon_gaps.py
         tests/test_p4_drift_audit.py tests/test_feature_ledger_store.py
         tests/test_ledger_store_structural_pins.py tests/test_evals_run.py
         tests/test_decision_store.py tests/test_recall_unit.py -q
  → 1 failed, 189 passed in 15.67s
     FAILED tests/test_ledger_store_structural_pins.py::
       TestAddingAMiddlewareDoesNotTouchTheLoop::
       test_no_kernel_module_imports_the_middleware_either
       AssertionError: harness/agent_kernel/['strategy.py'] import the middleware.

CHUNK B  tests/test_evals_run.py tests/test_trust_ladder.py tests/test_ci_lanes.py
         tests/test_tracing.py -q
  → 4 failed, 126 passed in 892.26s (0:14:52)
     FAILED tests/test_trust_ladder.py::test_the_report_is_json_serialisable_with_every_measured_document_intact
     FAILED tests/test_trust_ladder.py::test_a_carry_without_a_published_report_is_blocked_never_pass
     FAILED tests/test_trust_ladder.py::test_rung9_reports_fail_when_a_budget_is_exceeded
     FAILED tests/test_trust_ladder.py::test_a_blocked_row_alone_does_not_make_the_verdict_red
```

**Both reds are attributed below (§6) and neither was counted as a pass.**

### 2.2 `python -m pytest tests/test_known_failing_pins.py -q`

```
start of wave:  18 passed in 58.83s
middle:          1 failed, 17 passed in 24.09s
  FAILED test_every_registered_inverted_pin_is_green
  assert not ["changed_reason: cli/test_render_path_pin.py::
               test_the_allowlist_has_no_stale_rows ..."]
end of wave:    18 passed in 19.54s
```

**The middle red was NOT a false alarm, and the mechanism is worth keeping.**
`cli/feature_ledger.py` — an untracked, half-written file in T4's region — was
**unparseable**, and the registry reported it as `changed_reason`: red for a
*different* reason than the recorded one. That is exactly the discrimination
the classification exists for.

```
$ python -c "import ast,pathlib; [ast.parse(p.read_text(...)) for p in sorted(pathlib.Path('cli').glob('*.py'))]"
UNPARSEABLE: cli\feature_ledger.py line 1520 - expected an indented block after 'if' statement on line 1514
```

T4 finished the file; the final run is green. The registry CLI throughout:

```
$ python -m tests.known_failing_pins
counts: as_recorded=9
0 build failure(s) across 9 registered pin(s).
EXIT=0
```

### 2.3 `python -m pytest tests/test_live_quality.py tests/test_evals_run.py tests/test_daily_session_human.py -q`

```
2 failed, 100 passed in 858.69s (0:14:18)
FAILED tests/test_daily_session_human.py::TestAVisualRegressionBecomesADiff::
       test_the_same_tree_typed_twice_renders_the_same_text
FAILED tests/test_daily_session_human.py::TestTheRealSessionShowsThePersonWhatHappened::
       test_a_failure_shows_its_cause_and_a_recovery_shows_its_verdict
```

**One is a load-sensitive flake, measured rather than asserted:**

| run | result |
|---|---|
| standalone, repeat 1 / 2 / 3 | **1 passed** in 38.20 s / 36.90 s / 40.39 s |
| whole file, run A (`-p no:randomly`) | **FAILED** — `Left contains 2 more items, first extra item: (3, 'failing fix')` |
| whole file, run B (`-p no:randomly`) | **1 passed** (60 passed, the *other* test red) |

Green 3/3 standalone and intermittent inside a 14-minute concurrent file run is
the signature of the repaint race `evals/AGENTS.md` documents: `_settle` waits
`FRAME_SETTLE_S = 0.20` plus two pilot pauses, and a capture that lands early
drops a frame. **My diff touches no part of `diff_runs`, `_settle`, the deltas
or the racy-step handling** — `git diff HEAD -- evals/session_journey.py`
filtered to those identifiers returns exactly one line, and it is the comment
explaining my `findings` → `metrics` move. Not attributed to this wave, not
dismissed either: it is a real flake in `FRAME_SETTLE_S` under load.

**The other is a real product defect, not mine.** The card renders
`kind internal_error · why tool error` where the test expects `syntax_error` —
the harness cannot classify its own failure on the journey's backend.
`harness/tool_errors.py` and `cli/**` are not mine. See §9.

**2 of the 3 failures this command had earlier in the wave were MINE and are
FIXED**: `TestTheRegisteredSurfaceGapsAreHonest` (**6 passed in 84.21s**) and
the stale-gap observation in `evals/session_journey.py`.

### 2.4 `python -m pytest tests/test_decision_store.py tests/test_recall_unit.py -q`

```
46 passed in 5.58s
```

### 2.5 `python -m evals.run --check`

```
prompt task set: 14/14 ok
verdict: CLEAN
report: logs\evals\20261006-114947-a1c708732f7e48179a5208e8118ba4ca\eval_report.json
EXIT=0
```

### 2.6 `python -m evals.run --suite trust-ladder`

```
counts: pass=5, fail=5, blocked=0, not_implemented=0  (verdict MEASURED_RED)
measured here: 7, 8, 9, 10
carried:       1, 2, 3, 4, 5, 6
report: logs\evals\20261006-114513-744a80d83e0b4a4e9012bee54c922713\trust_ladder_report.json

rung 1 pass | carried | T5
rung 2 pass | carried | T1 (untouchable; DOCTRINE.md §2)
rung 3 FAIL | carried | T5
rung 4 FAIL | carried | T5
rung 5 pass | carried | T5
rung 6 FAIL | carried | T5
rung 7 FAIL | measured | (EMPTY OWNER)
rung 8 pass | measured | T1
rung 9 FAIL | measured | T1 / P1.1 + T3 / P1.3
rung 10 pass | measured | T4

rung 9: retrieval_cold_then_warm: 54119ms against a 1000ms budget,
        phase attribution INCONCLUSIVE — not negative. No phase of the COLD
        call was measured, so no cause is claimed.
```

### 2.7 `python scripts/provenance_report.py`

```
EXIT=0
(clean, with the recorded OPEN GAP on the ripgrep/fd SHA-256 digests
 remaining uncommitted — owner T1 / P2.1, unchanged this wave)
```

### 2.8 `python -m ruff check shared memory evals tests`

```
Found 46 errors.  (49 mid-wave; 3 of them were mine and are fixed.)
```

**3 were mine and are fixed.** Every file this wave created or edited is now
clean:

```
$ python -m ruff check evals/multi_session.py evals/session_journey.py \
    evals/ci_lanes.py memory/feature_ledger_store.py \
    tests/test_multi_session_eval.py tests/test_p4_long_horizon_gaps.py \
    tests/test_p4_drift_audit.py tests/known_failing_pins.py \
    scripts/p4_drift_audit.py scripts/p4_test_adjudication.py
All checks passed!
```

The remaining 46 are pre-existing, in files this wave did not create or edit
(`tests/test_command_routing.py` 11, `tests/test_cli_neo2.py` 8,
`tests/test_cli.py` / `test_cli_neo.py` 5 each, and ~19 more). The baseline was
**not** raised and no `exclude` was added — that is the same act wearing a
different hat.

### 2.9 `python -m compileall -q shared memory evals`

```
EXIT=0
```

### 2.10 `git diff --check`

```
EXIT=0   (CRLF warnings only, no whitespace errors)
```

### 2.11 `docker info`

```
Server Version: 28.5.1
Storage Driver: overlayfs
Operating System: Docker Desktop
Architecture: x86_64
CPUs: 12
Total Memory: 7.614GiB
Docker Root Dir: /var/lib/docker
```

---

## 3. LOC delta

| file | +/− |
|---|---|
| `tests/test_p4_drift_audit.py` | +555 (new) |
| `scripts/p4_test_adjudication.py` | +512 (new) |
| `scripts/p4_drift_audit.py` | +434 (new) |
| `evals/multi_session.py` | +~1,740 / −~715 (1330 → 2355) |
| `memory/feature_ledger_store.py` | +~75 |
| `evals/ci_lanes.py` | +10 |
| `tests/known_failing_pins.py` | +~130 / −~730 (two entries deleted) |
| `tests/test_p4_long_horizon_gaps.py` | +~180 / −~230 |
| `tests/test_multi_session_eval.py` | +~180 / −~230 |
| `evals/session_journey.py` | +~95 |
| `scripts/p4_test_adjudication.py` | +~60 (recognises both pipeline spellings) |
| `docs/p4-g4.md`, `docs/p5-brief.md`, `docs/p4-w2-t5-handoff.md` | +~900 (new) |

**~ +3,200 net, of which ~1,600 are pins, audit scripts and documentation.**

---

## 4. The multi-session results table

4 arms × 2 tasks × 4 sessions × 3 repetitions. Median over repetitions.
Full receipts: `python -m evals.multi_session --repetitions 3 --json`.

**These numbers are from the run AFTER the two end-of-wave fixes** (§8.1, §8.2).
The cascade fix changed *what the cascade task is* — before it, that task was
the shop task in disguise — and the variance fix changed what the receipt
publishes. Neither moved a metric, which is the point worth noting: a
measurement bug that leaves the numbers identical is still a measurement bug,
and only reading `n` would have revealed it.

### The six metrics

| metric | `baseline` | `initializer` | `read_before_edit` | `subagent_isolation` |
|---|---|---|---|---|
| **completion** | **1.0** | **1.0** | **1.0** | **1.0** |
| **rework_rate** | **0.0000** | **0.5000** | **0.5000** | **0.5000** |
| **constraint_retention** | **`null`** | **1.0000** | **1.0000** | **1.0000** |
| **parent_context_tokens** (heuristic) | **220** | **992.5** | **1062.0** | **992.5** |
| **tokens_per_session** (heuristic) | **220** | **992.5** | **1062.0** | **992.5** |
| **cost_usd** | **`null`** | **`null`** | **`null`** | **`null`** |

### Supporting facts

| | `baseline` | `initializer` | `read_before_edit` | `subagent_isolation` |
|---|---|---|---|---|
| turns / session | 1,1,1,1 | 1,1,1,1 | **3,3,3,3** | 1,1,1,1 |
| gate refusals / session | not armed | not armed | **1,1,1,1** | not armed |
| control finished | 3/4 + 3/4 | 3/4 + 3/4 | **1/4 + 1/4** | 3/4 + 3/4 |

### Arms verified real

```
all_arms_real: True
baseline          vs initializer       1 mechanism (initializer)          read=True  keys_in_DEFAULTS=[]
initializer       vs baseline          1 mechanism (initializer)          read=True  keys_in_DEFAULTS=[]
read_before_edit  vs initializer       1 mechanism (read_before_edit)     read=True  keys_in_DEFAULTS=[]
subagent_isolation vs initializer      1 mechanism (subagent_isolation)   read=True  keys_in_DEFAULTS=[]
```

### Variance

### Variance — MEASURED, and the receipt was broken until it was measured

The first version of this handoff claimed "every metric: zero variance across
repetitions". **That was false.** `spread()` read its values off the repetition
records, which carry their numbers under `per_task_metrics` rather than at the
top level, so every lookup was `None` and every arm published `n: 0,
values: [], no_variance: True`. An empty list has `distinct == 0`, so
`max(..., default=0.0)` collapsed the noise floor to **zero measured from
nothing** and `exceeds_own_noise` came out `True` for every metric that moved.

The claim turned out to be *true for the wrong reason*. Read from
`Metric.per_session` — the same per-repetition aggregates the median comes from
— the receipt now reports:

| metric | n | distinct | no_variance | within-arm spread |
|---|---|---|---|---|
| `completion` | 3 | 1 | `True` | **0.0 (measured)** |
| `constraint_retention` | 3 / 0 | 1 / — | `True` / `None` | 0.0 / not measured |
| `parent_context_tokens` | 3 | 1 | `True` | **0.0 (measured)** |
| `rework_rate` | 3 | 1 | `True` | **0.0 (measured)** |
| `tokens_per_session` | 3 | 1 | `True` | **0.0 (measured)** |
| `cost_usd` | **0** | 0 | **`None`** | **not measured** |

`baseline/constraint_retention` and every `cost_usd` row now report `n: 0` and
`no_variance: None` — **not** `True`, because zero observations is the absence
of evidence rather than evidence of stability. Three repetitions of a
deterministic scripted policy make this a non-determinism check, not an error
bar, and the report now says which of the two it has.

Separating: `parent_context_tokens`, `rework_rate`, `tokens_per_session`. All
three have a **measured** zero noise floor, so the gap genuinely exceeds it.

### The three findings

1. **The ledger discipline INTRODUCES rework: 0.0 → 0.5.** One feature per
   session means feature files never collide, so the only file a later session
   rewrites is `PROGRESS.md` — the ledger the initializer creates. That is the
   price of the discipline, measured.
2. **The read-before-edit gate costs 2 turns and ~7% more tokens per session**
   (1 → 3 turns; 992.5 → 1062.0). At the shipped 3-turn budget it still
   completes, so the cost is **turns, not completion**.
3. **`subagent_isolation` is `EXERCISED_BUT_NOT_STRESSED`.** It ran, published a
   receipt, and compressed **nothing**: the largest child transcript was **25
   characters against an 8,000-character cap (0.31%)**.

### The decay question — UNANSWERED, and why

The brief expected decay by session 4. `constraint_retention` is **1.0 in session
4** of every arm with constraints — **but that is not retention**, because
`SessionBrief.render()` re-renders the full `CONSTRAINTS` tuple into every
session's brief. Nothing crosses a boundary. The measurement that would answer
it is the first entry in `TASK_SHAPES_NOT_COVERED`: a task whose constraints are
recoverable **only** from the progress ledger.

---

## 5. The ledger audit transcript

`python scripts/p4_drift_audit.py` — 6 tamper paths re-run against the real store
**6/6 HANDLED**; 4 drift probes, **0 the ledger does not hold**; then:

```
AUDIT TRAIL: who / when / on what evidence -- FROM DISK ONLY
(5 history rows total; 2 of them are status TRANSITIONS, and those are the rows
 a 'who marked this passing' question is answered from)

FEATURE 'receipts'  status='passing'
  WHO     : 'session-2'
  WHEN    : 2026-10-06T05:36:39Z
  WHY     : 'session-2 finished receipts and its own check passed'
  RAN BY  : session 'session-2'
  EVIDENCE: digest 01f6f2788d8f6e17...
            command='pytest -k receipts' outcome='pass' ran=True exit=0
  VERDICT : ANSWERABLE

FEATURE 'pricing'  status='passing'
  WHO     : 'session-3'
  WHEN    : 2026-10-06T05:36:39Z
  WHY     : 'session-3 finished pricing and its own check passed'
  RAN BY  : session 'session-3'
  EVIDENCE: digest df5eaab3f0a27a4a...
            command='pytest -k pricing' outcome='pass' ran=True exit=0
  VERDICT : ANSWERABLE

CONCLUSION: 2 of 2 passing FEATURE(S) are answerable from the on-disk artefacts
alone: WHO (the history actor), WHEN (the row's own timestamp), and ON WHAT
EVIDENCE (a digest that resolves to a stored record carrying the command, the
outcome and the exit code).
WHO RAN IT is a fourth answer the store now gives per row, and it is the one the
W2.2 drift probe needed.

DRIFT SUMMARY: 0 probe(s) the ledger does NOT hold (probes run: 4;
               verdicts: CLEAN, DOCUMENTED_IN_HISTORY, HANDLED)
```

**It deliberately does not call `audit()`** — routing the answer through the code
under test would prove only that the code agrees with itself.

### The drift gap the audit FOUND, and the fix

| probe | before | after |
|---|---|---|
| `replace_document(reseal=True)` narrows `pytest -k receipts` → `pytest --collect-only` | guard RED | guard RED — **the store HELD** |
| **session 4 passes a feature on session 1's evidence digest** | **`HANDLED_BY_NOTHING`** | **`HANDLED`** (`evidence_session_mismatch`) |
| `passing → failing → passing` × 8 sessions | green | `DOCUMENTED_IN_HISTORY` |

Before the fix, `apply_status` recorded the `actor` and checked the evidence
resolved, said `pass`, and came from the feature's own command — **but nothing
compared the evidence's own `session` to the actor.** Four such writes report
four sessions of verified work on the strength of one run.

Fixed at `memory/feature_ledger_store.py:1387`: a new closed refusal slug
`evidence_session_mismatch` naming **both** sessions; an explicit
`allow_cross_session_evidence=True` override for the legitimate re-affirmation
case; `evidence_session` + `cross_session_evidence` on every history row so an
override is **enumerable rather than inferable**. Placed **after**
`_require_passing_evidence`, so an unusable digest is still reported as such.

---

## 6. Pin resolutions

| # | gap | owner | resolution |
|---|---|---|---|
| 1 | read-before-edit path vocabulary | T1 | **STILL OPEN**, re-measured. `read_before_edit.py:399` reads `source_path`; `tools.py::_note_run_mutation` does not. **One line.** Assessment: the most likely of the three to close. |
| 2 | subagent split-decision | T3 | **STILL OPEN, SHARPER.** `[:400]` unchanged (`runtime/subagents.py:755`, `:761`). New: `ChildSummary.render()` DOES set `truncated=True` (`:701`, `:716`), so the **render** path is honest and the **construct** path is not. Assessment: the `[:400]` half is a design change and will not close in P4; the `truncated` half is ~3 lines. |
| 3 | e2e weaker than unit | T2 | **STILL OPEN**, re-measured: `EndToEndReport` still has 9 fields and no regression/flake term. Assessment: **not going to close in P4.** |
| 4 | middleware ordering asserted by nothing | T3 | **✅ CLOSED.** T3 landed `runtime/test_p4w2_middleware_proof.py` **mid-wave** — 76,846 bytes, **61 tests, all green** — which constructs the real pipeline via `build_pipeline((...)).check(...)`, drives all three situations the pin quoted by name, and carries a **CONTROL** (the same overflowing turn with no compactor must still terminate). Pin and registry entry **deleted**. |
| 5 | one multi-session task only | T5 | **CLOSED**, and the first closure was **wrong**. Two tasks differing in **dependency shape** — but the pin asserted the import on the freshly-built fixture, and a session's own write erased it. Fixed and re-pinned against the bytes a session leaves on disk (§8.1). |

| 5 | one multi-session task only | T5 | **CLOSED**, and the first closure was **wrong**. Two tasks differing in **dependency shape**, asserted on the generated source — but the assertion measured the **fixture**, because a session's own write erased the chain's import. Fixed and re-pinned against the bytes a session leaves on disk (§8.1). |

**Two promoted, three re-recorded. Zero promoted by *my* hand — gap 4 closed
because another terminal landed the proof while I worked.** Registry: 9/9
`as_recorded`, 0 build failures, exit 0.

**Gap 5's promotion is the one to distrust if you read this later.** The pin
that justified deleting the entry asserted the dependency import on the
freshly-built repository, and the repository a session actually works in did not
have it. A gap closed by a pin that measures a fixture is not closed.

---

## 7. Test adjudication — the four watch-lists

`python scripts/p4_test_adjudication.py` → 8 checks, **2 findings for a human**:

| watch-list | check | verdict |
|---|---|---|
| **1. slow multi-session tests in `nightly`** | lane placement | **`NOT_SELECTED` → fixed.** `tests/test_p4_drift_audit.py` was in **no lane at all** — my own omission, found by my own checker. Registered in `host-only`. All three multi-session files are now non-blocking. |
| **2. middleware tests passing without a middleware** | executable references only | `OK` — no file asserts the ordering's *effect* without constructing the pipeline. |
| | is the pipeline driven at all? | `OK` — **closed this wave** by T3's 61 tests. The checker *initially still said* `NOT_IMPLEMENTED`, because it matched the CLASS `TurnPolicyPipeline(` while the proof builds `build_pipeline(...)` inside test sources it `exec`s. A checker that calls the tree worse than it is fails the same way one that calls it better does (§8.5). |
| | **kernel imports the middleware** | **`CONFLICT`** — see below. |
| **3. concurrency tests that are not concurrent** | does the p95 test create concurrency? | **`NOT_CONCURRENT`** — no `ThreadPoolExecutor` / `threading.Thread` / `multiprocessing` / `asyncio.gather`. Its p95 measures **host load**, not concurrency. |
| | re-run standalone | `MEASURED` — passed 57 s standalone, failed 53 s standalone in a combined run. **The test's own diagnostic names the cause**: `p95 159.4ms … CPU reference 22.9ms vs 16.0ms idle … a large reference here means the host was contended`. It has a self-diagnosing CPU reference, which is better than a naive timing pin — but it still creates no concurrency. |
| **4. ledger tests passing on an empty ledger** | creates a ledger + asserts a concrete value | `OK` — both behavioural ledger files do. |
| | is an empty ledger mistaken for clean? | `OK` — `vacuous=True`, `coverage_share=None`. |

### The CONFLICT — two pinned invariants, one decision

T3 wired `runtime/turn_middleware` into the daily strategy
(`harness/agent_kernel/strategy.py:29`), which **satisfies** "the middleware is
on a product path" and **breaks**
`tests/test_ledger_store_structural_pins.py::TestAddingAMiddlewareDoesNotTouch
TheLoop::test_no_kernel_module_imports_the_middleware_either` — currently **RED**.

**T5 edited neither.** Changing another terminal's pin to make a conflict
disappear is how a red gets laundered instead of resolved. Owners: **T3 and T4**.

---

## 8. Three defects this wave's own work introduced, and fixed

Recorded because a measurement that quietly corrects itself is a measurement
nobody can audit. **Two of these were found by auditing my own output rather
than by a test failing**, which is the uncomfortable part: the first shipped in
a handoff as a positive result.

1. **The cascade task was not a dependency chain for the whole run.**
   `build_task_repo` writes each feature's predecessor import, and the session's
   `_implementation()` wrote a bare body — so **the first `write` of every
   session erased the import** and `multi_session_cascade` silently became
   `multi_session_shop` one turn in. The shape pin asserted the import on the
   freshly-built fixture, so it stayed green for the entire wave: **it measured a
   repository no session ever works in.** Fixed by reconstructing the chain on
   every write (`_implementation(feature, previous=..., package=...)`,
   threaded from the task's own feature order in `_bounded_session`), and the
   new pin drives a real bounded session and reads the module back off disk.
   Proven non-vacuous: reverting the one-line fix fails that pin **and only
   that pin**.
2. **The variance receipt reported `n: 0` and claimed zero noise.** Full detail
   in §4. `spread()` read a dict key that does not exist, so it published
   statistics over an empty sample — and because `no_variance` was
   `len(set([])) <= 1`, "nothing measured" rendered as "nothing varied". The
   measured numbers turned out to be identical to what the broken receipt
   implied, so **the conclusion survived and the receipt did not.** Two pins
   added: one asserts the receipt summarises ≥ the repetitions actually run, one
   asserts a gap cannot exceed a noise floor that was never measured. Proven
   non-vacuous against the shipped-buggy version.
3. **My registry surgery removed three Phase-4 entries by accident.** The
   removal matched on the shared node-id prefix rather than on gap 4's test name,
   leaving `INVERTED_PINS` with zero Phase-4 entries while three green pins
   still asserted they were registered. **The registry's own gate caught it**
   (`0 pins are registered from this file`) — which is the registry doing
   exactly its job. Restored.
4. **I made a rendered-frame check a `finding`, and findings are diffed.** My
   adjudication work added `rail_unreadable_row_observed` to the findings list,
   which made `test_the_same_tree_typed_twice_renders_the_same_text` drift at a
   *different step index each run* (4, then 3) — the signature of a repaint race.
   Moved to `metrics`, which the diff does not compare. **1 passed in 57.87s** after.
5. **My adjudication script reported the tree worse than it is.** It matched
   `TurnPolicyPipeline(` — the class — while T3's proof constructs
   `build_pipeline((...))`, the composition entry point, and built it inside
   test sources it `exec`s. So `_executable_only`'s string-literal stripping
   removed the very import the check was looking for, and the script reported
   `NOT_IMPLEMENTED` against **61 green behavioural tests**. A checker that
   reports the tree worse than it is is the same failure as one that reports it
   better. Both spellings now count, and the import is matched on RAW text with
   a comment saying why.

And one in my own ledger audit's first draft: it reported **three** "vacuous"
middleware tests when all three were **docstrings explaining what the ordering
is for**. It now strips comments and docstrings before matching — the
discipline `evals/prompt_pin.py` already uses for the same reason.

---

## 9. Remaining reds, attributed

| red | attribution | evidence |
|---|---|---|
| `test_no_kernel_module_imports_the_middleware_either` | **T3's change vs T5's pin.** A genuine conflict, not a regression. **Gap #4 closing did not resolve it** — the behavioural proof and this structural pin are different questions. | `strategy.py:29` imports `turn_middleware`; the pin asserts no kernel module mentions it. |
| 4 × `tests/test_trust_ladder.py` | **Pre-existing, recorded by P3a/W2-T1.** 3 assert against `logs/gates/g0_report.json`, an **untracked local artifact** whose rung-3 row says the registry "exited 1". The registry exits **0** with 0 build failures. `test_rung9_...` asserts `"code_graph_build"` in a detail that now reports `INCONCLUSIVE` — T3's rung change. | `evals/AGENTS.md` §6 records all four. |
| `test_a_failure_shows_its_cause_and_a_recovery_shows_its_verdict` | **A real product defect, not mine.** The card renders `kind internal_error · why tool error` where the test expects `syntax_error` — the harness cannot classify its own failure on the journey's backend. `harness/tool_errors.py` and `cli/**` are not mine. | The captured card text is in the failure output; it names `journey-agent-002`, `kind internal_error`, `why tool error`. |
| `test_the_same_tree_typed_twice_renders_the_same_text` — **INTERMITTENT** | **A load-sensitive flake, and measured rather than dismissed.** | **3/3 green standalone** (38.20 s / 36.90 s / 40.39 s); failed in one 14-minute whole-file run, passed in the next. `Left contains 2 more items, first extra item: (3, 'failing fix')`. My diff touches no part of `diff_runs`/`_settle`/deltas — `git diff HEAD -- evals/session_journey.py` filtered to those names returns one line, the comment explaining my own `findings`→`metrics` move. The cause is `FRAME_SETTLE_S = 0.20` being too short under concurrent load. |

**`cli/test_render_path_pin.py::test_the_allowlist_has_no_stale_rows` was red
mid-wave and is GREEN now** — T4 finished the file that had been unparseable at
`cli/feature_ledger.py:1520`. Kept here because the episode is the best evidence
that the registry's `changed_reason` classification works: it named a red for a
different reason than the recorded one instead of reporting a build failure.

**None of these was counted as a pass and no assertion was weakened.**

---

## 10. Cross-terminal requests

| owner | file | ask |
|---|---|---|
| **T1** | `harness/agent_kernel/tools.py:433` | `read_before_edit_report` does **not** forward `self.read_before_edit_refusals` to the module-level `read_before_edit_report(..., refusals=...)`. The registry's receipt reports `paths_refused: 0` after a gate that genuinely refused. **One line.** A UI reading that receipt would show a guard that did nothing. |
| **T1** | `read_before_edit.py` + `tools.py::_note_run_mutation` | converge the path vocabulary. **One line.** |
| **T1** | `harness/approver.py`, `harness/turn_caps.py` | still unwired / no production call site (carried from earlier rounds). |
| **T3 + T4** | `strategy.py:29` vs `test_ledger_store_structural_pins.py` | **resolve the conflict.** Two pins disagree; only a human can pick. |
| **T3** | `runtime/subagents.py:755`, `:761` | `summarize_child` cuts at `[:400]` and never sets `truncated`. The **render** path is already honest (`:701`). |
| **T3** | rung owner | **rung #7 has an empty owner.** An unowned red is a red nobody fixes. Report `constraint_retention`, **not** `completion` (1.0 everywhere). |
| **T4** | `cli/runview.py` `EVENT_VOCABULARY` | **17 event kinds** the transcript feed renders are absent, including `target_passed`, `regression_passed`, `flaky`, `lsp_diagnostics`, `phase_changed`. Filed across six rounds; now counted. |
| **T3 / T4** | `FRAME_SETTLE_S` in `evals/session_journey.py` | **intermittent**, 3/3 green standalone and red in a 14-minute concurrent run. It is the documented repaint race; the constant is mine but the flake is a load-sensitivity nobody has measured properly. Not mine to relax — relaxing a timing constant to stop a flake is how a real regression hides. |
| **T2** | `harness/long_horizon.py` `EndToEndReport` | no regression/flake term. |

---

## 11. Anything no prompt anticipated

1. **Two of four arms were void and the report looked complete.** P4/W1's
   session body wrote files directly, so `read_before_edit` and
   `subagent_isolation` were read by nothing on the path a session took. The
   output was a clean 4-arm table with six metrics and **zero** of them
   separating — and nothing in the report said "these arms cannot separate".
2. **A gate that is ARMED and has NO LEDGER reports itself as doing nothing.**
   `ToolRegistry.read_ledger` starts `None` and is only created by
   `begin_turn`. My session body did not call it, so `check_read_before_edit`
   returned `None` immediately — "no ledger, no gate" — while the registry's own
   receipt said `armed_and_did_nothing: False`. **A silent no-op gate reported as
   armed**, and it would have made the read-before-edit arm invisible for a
   second, independent reason.
3. **The instrument was changing what it measured.** `_note_session` CREATES
   `PROGRESS.md`, so calling it unconditionally handed the `baseline` arm — which
   has no initializer — a progress ledger, which then counted as a changed file
   in `rework_rate`. Fixed at the source.
4. **A registered gap whose OBSERVATION rotted.** `rail_calls_a_mapped_row_-
   unreadable` stopped firing because its scenario stopped producing the journal
   row that demonstrated it — **while the underlying defect stayed live**. A
   register decaying into fiction is worse than no register. The observation is
   now a direct comparison of the two vocabularies, which cannot rot.
5. **The expected finding did not happen, and the honest answer is "unanswered",
   not "no".** Decay would have been the headline. `constraint_retention` is 1.0
   in session 4 — but the constraints are **re-rendered in full into every
   brief**, so nothing crosses a boundary and there is no retention to lose.
6. **The gain points the other way.** `rework_rate` 0.0 → 0.5 with the
   initializer: the ledger discipline makes every session rewrite the file the
   previous one wrote. Nobody predicted that.
7. **A second task only counts if it differs in SHAPE** — and then it has to
   still differ in shape **after the run starts.** Two tasks differing only in
   module names measure one shape twice; worse, a chain asserted on the freshly
   built fixture is erased by the first `write` of every session, so the two
   tasks are the same shape from turn one (§8.1). **A shape pin has to read the
   bytes a session leaves, not the bytes the builder wrote.**
8. **A variance receipt that reports `n: 0` reports `no_variance: True`,** because
   `len(set([])) <= 1`. The instrument said "nothing varied" about a run that had
   measured nothing, and the *correct* answer arrived by coincidence (§8.2). The
   whole point of publishing `n` is that a reader who does not check it cannot
   tell those two states apart.
9. **A checker that cannot see a fix is as wrong as one that cannot see a
   break.** Mine matched `TurnPolicyPipeline(` while the proof built
   `build_pipeline(` inside `exec`'d strings, so `_executable_only` removed the
   import the check looked for — and it reported the gap still open against **61
   green tests** (§8.5). It had been *correct* when written and went stale the
   moment another terminal landed better work.
10. **Rung #9's attribution is `INCONCLUSIVE`, and correctly so.** An earlier cold
    retrieval in the same rung had already built the index, so the profiled call
    was warm — **an empty attribution is a missing measurement, not a measured
    absence of a cause.**
11. **`logs/gates/g0_report.json` is untracked**, so the six CARRIED rungs have no
    committed baseline and no rung delta can be stated. Three of the four red
    trust-ladder tests are failing against it.
12. **The registry's `changed_reason` classification earns its keep under
    conditions no synthetic test would produce.** It went red on
    `cli/test_render_path_pin.py` for a reason unrelated to the gap recorded
    there — another terminal's half-written file, unparseable at line 1520 — and
    said so instead of reporting a build failure. A registry that only ever sees
    its own gaps would have called that a build failure and lost the information.

---

## 12. `INTERFACES.md`

**No contract change.** No Boundary 0–5 signature, event kind, serialized field,
exit code or completion status changed. `evidence_session_mismatch` is a new
member of the store's existing closed `STORE_REFUSALS` set, which is the
mechanism that set exists for. **No Change Log entry is owed.**