# Phase 1 — Daily Usable: the honest report

**Terminal:** T5 (contracts, memory, evals, CI)
**Phase:** P1 Daily Usable, Wave 1
**Written:** 2026-10-02
**Verdict:** see §6. The headline is `MEASURED_RED`, and that is a measurement,
not a mood.

> **The one sentence.** Phase 1's three deliverable guarantees — rungs #8, #9
> and #10 of the Trust Ladder — are now **executable and red**, and the red is
> the result. No number in this document is a claim about model quality.

---

## 1. What this phase is, and what "honest" costs

Phase 1 is a **performance** phase, and performance claims without measurement
are marketing. So the work in this wave was mostly *building the instruments*:
three rungs that can fail, a lane index that cannot drift, a provenance shape
for binaries, and this document.

The uncomfortable part, which the brief asked for and which is therefore in
§5 in the same breath as the wins: **one of those instruments found a real,
large, unnamed performance defect in a file I own**, and the rung it exposed
(the daily-use speed guarantee) is still red. A gate that is green because
nobody built the check is worse than no gate, because it is believed.

Everything below is measured on this host, on this checkout, at this commit.
Where a number is host-dependent it says so and names the machine.

---

## 2. The three rungs, and the machine every number came from

```
Windows AMD64 / cpus=12 / python 3.10.11
```

Run counts are stated with every figure. **A p95 is only ever printed over a
window of at least 20 samples**; below that the ladder relabels the number a
**median**, withholds the percentile, and prints the window it actually had.
A p95 over 3 samples is not a p95, and `evals/trust_ladder_rungs.Timing`
enforces that in the type rather than leaving it to the report author.

### Rung #8 — "stays coherent past 50 turns" → **FAIL**

Measured by two independent 55-turn runs of the **real** `harness.agent_loop`
through a scripted model on a real temporary repository. No Docker, no
network, no credential.

| what | measured |
|---|---|
| turns executed (run A) | **56** (`task_end.data.turns`) |
| real file edits | **55**, all distinct, **0 redone** |
| monotonic progress | **holds** |
| constraint planted turn 2, checked turn 45 | **NOT visible at turn 45** |
| turns on which the constraint was visible | **1 of 56** |
| turns that wrote the forbidden path as a result | **53** |
| declared turn ceiling | **60** (`harness/config.py`) — the ≥ 50 requirement is **met** |
| cap reported while the run is in progress | **no** |
| control arm: does the mechanism exist elsewhere? | **yes** — the core fix loop re-states its constraint on every tool result (`harness/core.py:3447`) |

T1 landed the turn-cap work **during** this wave. Re-measured on the new tree:
the ceiling is now 60, the ≥ 50 requirement is met, and the monotonic-progress
check passes. **Two failures remain, and both are real:**

1. **The interactive agent loop never re-states a constraint after turn 1.**
   Owner `T1 / P1.1`. The control arm matters: the mechanism demonstrably
   exists in the core fix loop, so this is a specific, nameable gap on the
   agent path — not "this repo has no such idea". T1 built
   `harness/turn_caps.py` and T1's own test says *"T4's rail and T5's rung-8
   read this"*; the mechanism exists and is not yet emitted from the agent
   loop.
2. **No journal row announces the ceiling while the run is in progress.**

**A false pass my own first version of this rung had, and the fix.** The
cap-receipt check originally substring-searched every journal row for
`max_turns` and came back **green** — on the `task_start` row, whose
`data.config` echoes the whole merged config. That is the run stating its
configuration at turn 0, which is a *different claim* from announcing that a
cap is approaching, and a check that passes for the wrong reason is worse than
one that fails. The check now requires a row that is not a start/end event
**and** that carries both a cap value and a remaining/approaching field. It
correctly reports `fail` on the current tree.

The failure mode the brief warned about — *silent degradation rather than the
run stopping* — is the one measured. The run completes 56 turns, makes 55 real
edits, reports `completed_unverified`, and has forgotten the rule it was given
by turn 3.

### Rung #9 — "fast enough to use daily" → **FAIL**

| metric | target | measured | statistic | window | machine |
|---|---|---|---|---|---|
| Startup → first token | < 2000 ms | **285 ms** | median | 5 runs | Windows AMD64 / py 3.10.11 |
| Startup → first token | — | 258–288 ms | min–max | 5 runs | same |
| Cold retrieval, needs a reindex | < 1000 ms | **5,613 ms** | median | 4 runs (deadline-bounded) | same |
| Cold retrieval | — | 5,454–6,176 ms | min–max | 4 runs | same |
| Warm retrieval | < 200 ms | **11.1 ms** | median | 20 runs | same |
| Warm retrieval | — | **14.7 ms** | p95 | **window = 20** | same |
| TUI frame cost | < 50 ms | **1 ms** | median | 5 runs | same |
| TUI frame cost | — | 1–4 ms | min–max | 5 runs | same |
| Directories the walk opens | — | **158** | count | 1 run | same |
| `search_repo` on this repo | — | 478 ms | single | 1 run | same |

**Before / after, with the run count and the machine**, because a number with
no baseline is a number with no meaning. Both columns are this ladder, this
machine, the same probes:

| metric | before (P1/W1 start) | after (P1/W1 end) | change | what changed it |
|---|---|---|---|---|
| Startup → first token, median of 5 | 872 ms | **285 ms** | **3.1× faster** | T1/T3's lazy-import work |
| Cold retrieval, median | 34,423 ms | **5,613 ms** | **6.1× faster** | T1's 45-entry shared skip set + literal prefilter |
| Directories the walk opens | 147,239 | **158** | **932× fewer** | T1's `harness/skipset.py` |
| Warm retrieval, median of 20 | not measured | **11.1 ms** | — | first measured this wave |
| TUI frame cost, median of 5 | 5 ms | **1 ms** | 5× faster | — |
| Cold retrieval vs its 1 s budget | 34× over | **5.6× over** | — | still red |

Read the retrieval line carefully, because the two numbers move in opposite
directions and the honest reading is not "retrieval is slow":

- **Warm retrieval meets its budget with room to spare** (11.1 ms median,
  14.7 ms p95 over a window of 20).
- **Cold retrieval — the one a user pays after any source edit — is 5.6×
  over budget**, and that is *after* a 6.1× improvement this wave. The
  dominant remaining cost is the tree-sitter code graph build, and the
  diagnosis is stated per-phase rather than inferred.

**The control arm, and the first diagnosis I got wrong.** The first version of
this rung reported "the skip list does not cover this repository", from a
>10× ratio against a control walk. **That was a lie.** What actually happened:
T1 landed a 45-entry shared skip set (`harness/skipset.py`) while this probe
was being written. The walk went from **147,239 directories to 158** — a 932×
reduction — and the time that remained had nothing to do with the walk.
`attribute_retrieval()` now reports the measured dominant phase *and whether
the graph was rebuilt*, so an attribution from a warm run can never be quoted
as a cold one, and when no phase exceeds 50% of the total it says **no cause**
rather than naming a culprit.

**The defect I found and fixed** (`memory/code_graph.py`, my file):
`_resolve_calls` resolves each call site's enclosing symbol through
`_caller_id`, and the previous R2-09 optimisation had indexed only the
*symbol-level* branch, leaving `if caller_qualified == module:` doing a full
linear scan of `graph.nodes`. With one module-level call site per file times
20,065 nodes, that was O(files × nodes).

| | before | after |
|---|---|---|
| `_caller_id` | **134,069 calls, 34.7 s** | **1.1 s** |
| `str.startswith` calls it drove | **55,436,109** | not in the top phases |
| `retrieve_context` cold, profiled | 93.2 s | 52.0 s |
| forced full graph rebuild | 88.7 s | **24.6 s** |

And the reason it survived a pin: the test that pinned it used a **4-file,
8-node fixture comparing 4 call sites**. An O(nodes)-per-call-site scan is free
at that size. `tests/test_code_graph_caller_index.py` now pins the same
equivalence against the linear-scan oracle on a graph of ≥ 40 nodes and ≥ 12
call sites, asserts the module-level branch specifically, and includes a
**scaling** test — because a correctness test cannot catch a complexity
regression, and that is the whole defect class here.

**What is still red and why.** The residual cold cost is a full tree-sitter
reindex of 579 files / 20,065 nodes, plus a large share spent serialising the
persisted graph to JSON. `memory/code_graph.py` has **no incremental index** —
its own docstring says so — so a single source edit forces a whole-repository
rebuild. That is not a P1 speedup; it is a P3a/P4-scale piece of work.
**Rung #9 stays `fail` with that named, and the named owner is `T1 / T5`, not
"the box was slow".**

### Rung #10 — "you can always see what it did and why" → **FAIL**

Measured on a real `run_agent_stepped` run that edited two files, with every
link in the chain read from real artifacts.

| link | status | what happened |
|---|---|---|
| 1. journal row | **present** | `logs/{task}/trace.jsonl`, 20 events, 12 distinct kinds |
| 2. `shared.traceview` | **present** | 6 keys: `ts, module, event, source, task_id, data` |
| 3. `cli.runview` | **present but lossy** | 48 run facts — **`module` and `source` are both dropped** |
| 4. TUI card | **present** | `ResultCard.render()` produced a real frame |

The chain runs. It **loses information at link 3**, and that is the defect
this rung exists to find.

**Honest presentation — three findings, all measured on the rendered view:**

1. **A truncated search is invisible.** A `retrieval_truncated` journal row
   (with `truncated=True`, `truncation="budget"`, `not_searched=12`) produces
   **no run fact and no card line**. It is recorded, it is visible to
   `shared.traceview`, and then it is dropped. `harness/knowledge.py::render_
   truncation_note` exists and has **no production call site**;
   `cli/runview.py` lists `retrieval_truncated` in `INFORMATIONAL_EVENTS`, so
   it is consumed and never surfaced. A reader is told a search was
   incomplete by nothing at all. Owner **T4**.
2. **An absent value has no vocabulary on the card.** `cli/runview.py::briefing
   _lines` already has the convention (`if not data.get("available")` → one
   honest line). `card_lines` and `ResultCard` have none, so an absent field is
   omitted and reads as "there was nothing to report". Owner **T4**.
3. **A vacuous zero is rendered as a dollar amount.** A run that charged
   **nothing** (`cost_known=False`, `cost_usd=0.0`) renders `$0.000000` with no
   `unpriced` and no `vacuous` marker. The **charged control arm was rendered
   in the same pass** and came out `cost_usd=0.0 / cost_known=False` too — so
   the two arms are not distinguishable in the rendered view either. That
   second fact is itself a finding: on this path cost is derived from the
   sandbox boundary, so a scripted run cannot charge at all, and the card has
   no way to say so. `DOCTRINE.md` §1 forbids reporting an unpriced value as
   `$0`, and §3 requires `vacuous: true` beside a zero that measures nothing.
   Owner **T4**.

**Rung #10 includes honest presentation, not just honest data** — the brief's
point. The data is in the journal. The presentation is what is missing.

---

## 3. The six labelled CI lanes

`evals/ci_lanes.py` is the **authority** (six lanes, as data) and
`tests/test_ci_lanes.py` fails when the workflows in `.github/workflows/`
disagree with it. A lane list written only in YAML is a list nobody checks.

| lane | budget | Docker | network | blocking | where |
|---|---|---|---|---|---|
| `smoke` | 1 min | no | no | **yes** | `ci-lanes.yml#smoke` |
| `trust-ladder` | 5 min | no | no | **yes** | `ci-lanes.yml#trust-ladder` |
| `host-only` | 15 min | no | no | no | `ci-lanes.yml#host-only` |
| `docker` | 30 min | yes | yes | **yes** | `ci-lanes.yml#docker` |
| `windows` | 45 min | no | no | **yes** | `windows-dockerfree-ci.yml#windows-dockerfree` |
| `nightly` | 200 min | yes | yes | no | `nightly-quality.yml#live-quality` |

**The known-failing pin registry gates every one of the six.** Not "the lanes
that run the pins" — every lane runs `python -m tests.known_failing_pins`
before its first test invocation, and its exit code is the job's. A registry
some lanes read and others ignore is a registry whose promoted pins are
promoted in one job and invisible in five, which is the silent rot it exists
to stop.

**The registry also catches a pin failing for the wrong reason**, which is a
regression rather than a closed gap and is the outcome most dangerous to file
as a known failure. Pinned on synthetic observations in
`tests/test_ci_lanes.py`, so it does not need the real tree to contain the
case. The four outcomes it distinguishes: `as_recorded`, `promote_me` (the
gap closed — a build failure carrying the word PROMOTE), `changed_reason`
(regression), and `missing` / `not_collected` / `timed_out` (a registry entry
that cannot be observed is not a recording).

**Host-dependent timing does not block.** The `host-only` lane holds the SLO
and 5,000-session p95 pins and is deliberately **non-blocking**, because a
flaky timing test in a blocking lane trains people to ignore the gate. The one
declared exception is `trust-ladder`, whose rungs are budget assertions **by
construction**; it is named in the code and asserted by a test, not implied.

**The Windows lane runs the `cli/`-local modules T4 added** —
`cli/test_cli_import_smoke.py`, `test_display_contract.py`,
`test_render_path_pin.py`, `test_sanitize_pipeline.py`,
`test_sanitize_shapes.py` — enumerated, never globbed.

**A real defect this found.** `tests/test_ci_lanes.py` extracts every
`shell: python` step from every workflow and `ast.parse`s it, because a
syntax error in one of those steps otherwise shows up only as a red Windows job
after merge. It found a live **`IndentationError`** in
`.github/workflows/release-gate.yml`: three lines in a `shell: python` step
were indented 11 spaces inside a 10-space block. Fixed.

---

## 4. Provenance: the binary shape

`scripts/provenance_report.py` runs **clean** over the whole tree:
`PROVENANCE_COMPLETE`, `BINARY_PROVENANCE_COMPLETE`, `declared: 0`,
`undeclared: 0`, `mismatched: 0`. Exit 0.

**The binary shape is a different shape, not a different field list.** A source
file declares itself in a comment header, because a source file can carry a
comment. A downloaded binary cannot: it is bytes. So binaries are declared in
`provenance/binaries.json`, and the scanner **re-hashes the file on disk** and
fails on a mismatch — a swapped binary is a build failure, not a surprise.

| | source shape | binary, committed | binary, runtime-fetched |
|---|---|---|---|
| `upstream` | required | required | required |
| `upstream-commit` | **required** | not applicable (a release archive is not a checkout) | not applicable |
| `version` | — | **required** | **required** |
| `sha256` | — | **required, re-checked on disk** | not required — there are no bytes in the tree to re-hash |
| `platform` | — | **required** (`os/arch`, so one entry cannot appear to cover all) | required, as a list |
| `digest-source` | — | — | **required** |
| `licence` | required | required | required |
| `beats-ours-because` | required | required | required |

A dual-licensed upstream must name the licence **actually relied on**.
`licence: "MIT or Apache-2.0"` is **rejected**, because a dual-licensed
dependency with an unstated choice is an unanswerable licence question.

**Licences, verified against the projects' own files on 2026-10-02 — and the
brief's statement was wrong:**

| dep | actual | recorded | note |
|---|---|---|---|
| ripgrep | **MIT OR Unlicense** (dual) | MIT | the brief said "ripgrep is **MIT**" — an underspecification. MIT is chosen because it carries an attribution-and-notice obligation `NOTICE` can discharge |
| `fd` | **MIT OR Apache-2.0** (dual) | MIT | Apache-2.0's patent grant was not needed for a file-listing subprocess; one licence determination across all artefacts beats two |

`NOTICE` records both, with the upstream copyright lines reproduced, and
records Aider's Apache-2.0 attribution obligation **now, before the port** — a
provenance mechanism introduced alongside the first port is a mechanism that
has never been wrong yet. `NOTICE` §3 explicitly states that **nothing from
Aider is copied into this tree as of this notice**, because it is not.

**The open gap, named rather than left to be found.** For both binaries the
**version** is pinned in `harness/search_engine.py` (`_RG_VERSIONS` /
`_FD_VERSIONS`) and the **expected SHA-256 is not committed anywhere in this
repository**. The downloader fetches the `.sha256` sidecar GitHub publishes
beside each release asset and compares before extracting — the right thing at
download time, and **not** the same thing as committing the digest: a replaced
asset with a replaced sidecar would be accepted. The gap is machine-readable
at `provenance/binaries.json` → `fetched_binaries[].digest-unpinned-reason` and
prints under `OPEN GAP`. Owner **T1 / P2.1**.

**A false positive the binary lane produced, and the honest fix.** Its first
run reported two `.wasm` files under `site/.next/server/edge-chunks/` as
undeclared vendored binaries. They are Next.js build **output** — the
equivalent of the `dist`/`build` entries already excluded. The fix was to
exclude `.next`/`out` and **record why in the file**; loosening the `.wasm`
suffix would have been the bug. A test pins the reason's continued presence,
because an exclusion with no recorded cause is an unexplained hole.

---

## 5. The honest cost — read this with §2, not after it

**The turn ceiling went 25 → 60.** That is T1's landed change, measured in this
wave, and it is **2.4×**, not 2×.

| | before | after | ratio |
|---|---|---|---|
| per-task tool-calling turns | 25 | **60** | **2.4×** |
| worst-case model calls per task | 25 | 60 | 2.4× |
| worst-case token burn under a fixed `budget_cap_usd` | 25 turns' worth | 60 turns' worth | up to 2.4× |
| worst-case wall clock per task | 25 turns' worth | 60 turns' worth | up to 2.4× |
| typical case (a task finishing in 8 turns) | 8 | 8 | **unchanged** |

**What that costs you, stated plainly.** If your tasks typically finish in
under 25 turns, nothing changes and you pay nothing. If any of your tasks were
being **truncated at 25 turns**, they now run to as much as 60 — and those are
precisely the tasks that were failing. So the extra spend is concentrated
entirely on runs that were previously cut short, which is the case where the
alternative was not a cheaper success but **no result at all**.

**The dollar worst case is capped elsewhere.** `budget_cap_usd` is read before
every model call (`harness/agent_loop_step.py:756`) and is independent of the
turn count, so turning the ceiling up does not by itself raise the maximum
spend per task. The turn count is the worst case for **latency, token burn
under a fixed cap, and provider quota** — not for the bill's ceiling.

**This is the same breath as the speed wins, deliberately.** §2 reports
startup at 872 ms and warm retrieval at 11.1 ms. Neither of those got cheaper
because the turn cap went up; the ceiling is a *reliability* change with a cost,
and the cost is 2.4× worst-case turns.

**And the honest framing of what is still red:** today a 56-turn run is
*refused* at 25 turns. Before T1's change, the state was a **1.7× latency
truncation**, not a 1× honest cost. Raising the cap converted a truncation into
real spend, and rung #8 is still `fail` because the agent loop does not
re-state a constraint — so the extra turns are currently being spent on a loop
that forgets its instructions. **Fix the decay before the cap pays off.**

---

## 6. What this phase did **not** establish

Stated plainly, because a verdict read without its limits is a different claim
from the one being made:

1. **`--check` is a 14-task prompt self-check.** It proves the eval harness is
   internally consistent and that no prompt change regressed a scripted arm.
   It is **not** a claim about model quality, model cost, or model latency.
2. **Every model call in every rung is a scripted double.** No live provider
   was reachable — T3 recorded `ServiceUnavailableError: No available channel`
   on 3/3 live completions. **No number in this document is a claim about
   model quality.** This sentence is repeated at the top of
   `evals.trust_ladder`'s own report, in `NOT_ESTABLISHED`, and in the CLI
   output, because it is the sentence most likely to be dropped when a figure
   is quoted.
3. **No live-provider lane ran.** A Docker daemon **was** reachable this wave
   (`docker info` → `Server Version: 28.5.1`, exit 0), but no Docker-backed
   eval lane and no live-provider lane was run by this work. Where a row is
   `blocked`, the exact reason is in the row.
4. **Rungs #1–#6 are CARRIED, not measured.** They point at a published G0 row
   or a source-level pin. A carried row is a **pointer**, not a number. In this
   checkout `logs/gates/g0_report.json` does not exist, so those rows are
   `blocked` with a runnable remedy — never `pass`. Rung #7 is the only rung
   with no gate behind it at all, and it is `not_implemented` with its owning
   phase named.
5. **Rung #9's retrieval figure is a claim about one checkout on one machine.**
   It is not a claim about the product on an arbitrary host. The control arm
   and the per-phase attribution exist so the number cannot be read without
   its cause — but the number is still a number about this box.
6. **The T1/T3/T4 P1 tests arrived mid-wave and were adjudicated — see §7.**
   At the *start* of this wave nothing had arrived: the newest file write in
   the whole repository was my own P0 work. T1's
   `harness/test_p1w1_performance.py` (47 tests) and T2's
   `execution/test_w1_latency_honesty_pins.py` (43 tests) landed at 04:03 and
   04:09. Both were read in full and both are **ACCEPTED** — see §7 for the
   P0 checks applied. No T3 or T4 test file was received.
7. **No release artifact was produced, no tag, no commit, no publication.**
   The tree is dirty and committing requires explicit human approval.
8. **The binary lane cannot see a binary fetched at *run time* by another
   module.** It sees committed binaries by suffix and manifest path. The
   runtime-fetch section is a **declaration the project makes about itself**,
   and its correctness is a review question, not a measurement.

---

## 7. Adjudicating the terminals' P1 tests

My brief said "T1, T3, T4 sent tests. **Read each before accepting.**" At the
start of the wave nothing had arrived (the newest file write in the repository
was my own P0 work). Two files landed mid-wave and were read in full. **Both
are accepted.** T3 and T4 sent nothing.

### T1 — `harness/test_p1w1_performance.py` (47 tests) → **ACCEPTED**

| P0 check | verdict | why |
|---|---|---|
| **Non-vacuity** | **passes** | Three tests are *shaped* against vacuity and say so in their own docstrings. `test_the_prefilter_never_drops_a_real_match` is a **differential** (17 patterns, prefilter on vs monkeypatched off) rather than an assertion that the prefilter returns something. `test_a_sound_pattern_still_yields_a_usable_filter` is the **control** for it, with the reasoning spelled out: *"a prefilter that always returned `""` would satisfy every soundness test above while delivering none of the speed, and would be a much more comfortable thing to ship."* That is the DOCTRINE §3 control-arm rule applied by its author, not by me. |
| **Absolute-claim avoidance** | **passes** | `test_logs_is_in_the_set_and_that_is_the_whole_345x` asserts **set membership**, not a time, and carries the measurement in its docstring (147,241 dirs / 71.6 s vs 271 / 0.08 s). **No host-dependent timing threshold anywhere in the file.** |
| **Windows viability** | **passes** | Runs green here on Windows in 3.60 s. The one-authority assertion is **object identity** across three modules, which is OS-independent. |
| **No vacuous pass by never charging** | **passes** | `test_the_engine_is_reported_on_every_outcome_including_every_refusal` walks the **refusal** paths, because *"the silent fallback this round removes lives precisely on the paths a test that only searches the happy path never reaches."* `test_an_untruncated_result_is_reported_complete` is the control for the truncation tests. |
| **Provenance** | **passes** | `test_the_provenance_header_names_the_source_and_the_licence` requires the SPDX tag, the upstream URL, a commit pin, and — the mandatory field — `WHY IT BEATS OURS` in the file's own header. That is the source-shape contract, enforced by its own author. |

**I pushed back on nothing in T1's file.** It is the strongest test file I have
read in this repository, and the reason is structural: every assertion that
could have been satisfied by a comfortable no-op has a named control beside it.

### T2 — `execution/test_w1_latency_honesty_pins.py` (43 tests) → **ACCEPTED**

| P0 check | verdict | why |
|---|---|---|
| **Non-vacuity** | **passes** | The driver docstring says the honesty rows must not depend on a container, *"because a suite that can only prove 'not_run is honest' when Docker happens to be up is a suite that stops proving it exactly when someone needs it."* The fake sandbox does `time.sleep(0.01)` on purpose: *"The stub must produce a non-zero duration, because a test that asserts `container_s > 0` against a stub that does nothing is testing that the clock ticks."* |
| **Absolute-claim avoidance** | **passes, and this is the important one** | The file has exactly **one** wall-clock assertion: `assert after < max(2.0, warm * 3)` — a **relative** comparison against the same-machine pre-edit baseline, with a generous 2.0 s absolute floor. That is the correct form of a host-dependent timing test: it asserts the *order of magnitude of the change*, not an absolute figure, so it cannot fail on a slower box merely because the box is slower. The other two quantitative assertions are **counts**, not times (`dirs_opened < 5_000`, `dirs_opened <= 4`), which are host-independent. |
| **Windows viability** | **passes** | 43 tests green here in 2.16 s, no Docker, no network. |
| **No vacuous pass by never charging** | **passes** | `test_the_fast_lane_reports_not_run_and_false_not_true` asserts the **control arm first** — the default full-suite lane must be `COMPLETE` and `True` — so the test below cannot pass because the lane is broken rather than because it is honest. |
| **Provenance** | n/a | No vendored code in `execution/`. |

### T3 (runtime) and T4 (cli) → **NOTHING RECEIVED**

No test file from either terminal arrived. This is recorded as an absence, not
as a pass. The four rung-#10 findings in §2 — the dropped `module`/`source`
attribution, the invisible truncated search, the missing `unavailable`
vocabulary, and the `$0.000000` for an uncharged run — are all in T4's files
and all have named owners, which is the actionable form of "nothing received".

### The full-suite state at the end of this wave

`python -m pytest tests/ -q -p no:randomly` → **7313 passed, 18 failed, 33
skipped in 3966 s (1:06:06)**. **None of the 18 is in a file this terminal
owns.** They are T1's (`test_config_trace_state`, `test_skills` ×2 — the
non-hermetic skills tests `scripts/AGENTS.md` records as passing on a clean
runner and failing on a machine that installed a plugin), T3's
(`test_scheduler_integration`), T4's (7 CLI/theme/plugin/aesthetic failures,
the docs-truth version drift, and the TUI visual-regression test), the
load-sensitive `test_ceiling03_sessions` p95 pin, and the known
`test_module_reachability` staleness. Each is reported as `fail` with its owner,
never as a skip.

---

## 7. Reproduce every number in this document

```powershell
python -m evals.run --suite trust-ladder        # the ten rungs; exit 2 while red
python -m evals.run --suite trust-ladder --json # machine-readable
python -m evals.ci_lanes                        # the six lanes, exit 2 on disagreement
python -m tests.known_failing_pins              # the registry, exit 2 on a promotion
python scripts/provenance_report.py             # provenance + binaries, exit 2 on a gap
python -m pytest tests/test_trust_ladder.py tests/test_ci_lanes.py `
                 tests/test_provenance_binaries.py `
                 tests/test_code_graph_caller_index.py -q -p no:randomly
```

The ladder's report is written to
`logs/evals/<run-id>/trust_ladder_report.json` and carries every measurement
document, not just the verdict.

---

## 8. Open items, with owners

| # | item | owner | why it is not mine |
|---|---|---|---|
| 1 | re-state a constraint on every turn of the **agent** loop (core fix loop already does; `turn_caps` already builds the receipt) | T1 / P1.1 | `harness/agent_loop_step.py` |
| 2 | emit the turn-cap approach receipt into the agent loop's journal | T1 / P1.1 | no such row exists in any loop |
| 3 | incremental code-graph index (the residual 34 s) | T1 / P5, phase-scoped | `memory/code_graph.py` is mine; the **design** is a P3a/P4 decision |
| 4 | carry `module`/`source` from traceview into the runview projection | T4 / P1.4 | `cli/runview.py` |
| 5 | surface a `retrieval_truncated` search on the card | T4 / P1.4 | `cli/runview.py`, `cli/tui_components.py`; `render_truncation_note` has no call site |
| 6 | give the card an `unavailable` / `unpriced` vocabulary | T4 / P1.4 | same; `briefing_lines` has the convention already |
| 7 | mark a vacuous cost zero as vacuous, not `$0.000000` | T4 / P1.4 | same |
| 8 | commit the eight `rg`/`fd` SHA-256 digests | T1 / P2.1 | `harness/search_engine.py` |
| 9 | `evals.gates` is an orphan module — nothing imports it; it is reached only via `python -m evals.gates.P0` | T5 / next round | pre-existing; recorded, not fixed this wave |
| 10 | `demo/test_product_docs.py` docs drift (20+ commands absent from the reference) | T4 | pre-existing, carried in `KNOWN_REAL_FAILURES` |

**Nothing in that table is in this document as a claim of success.** They are
the reasons three rungs are red.
