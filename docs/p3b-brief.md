# Phase 3b brief — what P3a proved, and what it therefore asks of 3b

**Read `docs/p3a-g3a.md` first.** This document is not a summary of P3a; it is
the argument for what comes next, and every premise in it is a measurement from
that document rather than an intention.

## The one sentence

P3a shipped the memory subsystem AND the instrument that says it does not yet
help, and the instrument found a **negative result with a real cost attached**:
memory was recalled, was not useful, and on the one task where it was acted upon
it turned a solved task into a failed one in all three repetitions.

That is the most valuable thing this phase produced, and 3b exists to act on it.

## The three numbers that set the agenda

| measurement | value | what it rules out |
|---|---|---|
| grounded resolve delta | `-0.071429`, intervals overlap | "memory makes the agent better at fixing things" — not on this task set, with this model |
| harm over moved outcomes | **3 of 3 for the worse** | "an inert memory is harmless" — it is not; an inert one that is READ is worse than none |
| staleness of the recalled corpus | `0.166667` | "the corpus is fine, ship it" — one in six recalled memories contradicts the code |

The cost/token axis is **vacuous** (every scripted call reports exactly 20
tokens), so it is on the list as a measurement to *build*, not a number to read.

## What 3b must do, in priority order

### 1. Make a detected-stale memory STOP reaching the model

This is the highest-value item in the phase and it is nearly free, because the
detection already works: `retraction.detection_rate = 1.0` over 5 cases.

**The gap:** `retraction.amended_count` is **0**, and nothing reads the staleness
flag. A memory that the snapshot knows contradicts the repository still arrives
in the planner's context and is acted upon — which is exactly the bug01_wrap
failure, in full.

3b's first deliverable is the CONSUMER: at recall time, drop or visibly mark rows
whose recorded snapshot is stale, and prove the harm rate moves. The success
criterion is not "we added a check"; it is
**`outcomes_moved_for_the_worse` going from 3 to 0** on the same 168 runs.

The negative control matters as much as the fix: run it with the staleness flag
forced ON for rows that are not stale, and confirm harm does not simply vanish
because the agent stopped reading memory entirely.

### 2. Make the cost axis real

The current delta is `$0.00` in both directions and always will be, because the
scripted double reports a flat 20 tokens per call. A memory subsystem whose cost
axis cannot move cannot be argued about.

3b needs a token estimator that reads the ACTUAL prompt length, and at least one
real-provider lane. Until then every cost claim in the tree is `cost_axis_vacuous:
true`, which is the honest state and a poor permanent one.

### 3. Turn recall precision from 0.667 into a number worth having

Two of six injected memories were irrelevant to the task they reached. That is
the number a ranking change has to move, and it is currently moved by nothing:
`memory_ranking_enabled` has exactly one consumer, which is the ablation arm
that measures it.

3b should wire ranking into a real recall path and then re-run this same report.
If precision does not move, the ranking work is not finished, and the honest
outcome is to say so rather than to re-label the metric.

### 4. Close the coverage gaps P3a named rather than hid

* 9 of 14 tasks recalled **nothing at all**. Either the corpus does not cover
  them, or retrieval does not reach it. Which one is a measurement, and it has
  not been made.
* `DecisionStore.ingest_state_file` promotes with no human in the loop. It is
  declared in `DECLARED_BYPASSES` and that is the right thing to have done with
  it, but declaring a bypass is not closing it.
* Consolidation writes `MEMORY.md` / `USER.md` and **nothing reads them back**.
  No `mcp_server` tool exposes `memory_search` or `memory_revoke`.

### 5. Rung #7 stays red until the corpus is trustworthy

`evals/trust_ladder.py::_rung7_cross_session_recall` now FAILS on a real number
instead of reporting `not_implemented`. It will pass when the stale rate is zero
AND the tasks that recall nothing are explained. Do not widen its criteria to
make it green — the criteria are the deliverable.

## What 3b must NOT do

* **Do not add a `harness/config.py` `DEFAULTS` key for any memory control.** A
  default is merged into every task and every eval arm at once. The P3a keys are
  read by key presence for exactly this reason and there is a test pinning their
  absence.
* **Do not widen the trust claim past the extraction pipeline** without also
  routing `ingest_state_file` through the inbox. The declared bypass exists so
  this boundary is legible; erasing it makes the gate less honest, not more
  secure.
* **Do not quote a point estimate without its interval.** The `-0.071429` with
  overlapping CIs is the finding. The number alone is an anecdote with decimals.
* **Do not write a second token estimator.** `evals.memory_ablation_real` has
  one, and `shared.security._SECRET_PATTERNS` is the only secret pattern table.
  Two answers to one question is a defect this repository has already paid for
  several times.

## Where the artefacts are

| what | path |
|---|---|
| the ablation (168 Docker-verified runs) | `logs/evals/p3a-memory-ablation-real.json` |
| the quality report | `logs/evals/p3a-memory-quality.json` |
| the inbox gate | `python -m evals.inbox_gate` |
| the G3a verdict | `python -m evals.gates.P3` |
| the Trust Ladder | `python -m evals.run --suite trust-ladder` |
| the G3a write-up | `docs/p3a-g3a.md` |

**No live provider ran at any point in P3a.** Every model call in every number in
either document is a scripted double, and neither document claims otherwise.
