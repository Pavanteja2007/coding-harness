# G1 — the Phase 1 release verdict

**Verdict: `G1_RED`.** Nine rows pass, SIX fail, two are `blocked`, one is
`not_implemented`. No row is `skip`; `skip` is not a member of the vocabulary
this document is written in.

Machine-readable source of every number below:
`python -m evals.gates.P1 --ladder-report <path> --demos-report <path>
--tests-log <path> --module-local-log <path>`
(the gate re-derives the cheap probes itself and exits 2 while the verdict is
red).

**What `G1_RED` means.** At least one blocking guarantee was *measured* and did
not hold. It is a published measurement, not a failed build. Relaxing a check, a
budget or a pin to turn this document green is forbidden by the brief that
created it, and the CI lanes were not edited to make any row pass.

**Machine:** `Windows AMD64 / cpus=12 / python 3.10.11`.

---

## 1. The performance table

Every number carries its machine, its run count, and whether it is a **median**
or a **p95**. `MIN_SAMPLES_FOR_P95 = 20`: below that window the headline is a
median and the percentile is **withheld**, because a p95 over 2 samples is not a
p95. No p95 appears in this report.

| metric | before | after | unit | runs | statistic | machine | budget | within |
|---|---|---|---|---|---|---|---|---|
| `startup_to_first_token` | not measured before Phase 1 | **415** | ms | 5 | median (336–463) | Windows AMD64 / cpus=12 / py3.10.11 | 2000 | yes |
| `retrieval_cold_then_warm` (cold) | **144.6 s** (`DOCTRINE.md` §8) | **19 380** | ms | 2 | median (19 336–19 424) | same | 1000 | **NO** |
| `retrieval_walk_dirs_opened` | **147 241** dirs | **164** | dirs | 1 | single observation | same | — | — |
| `tui_frame_cost` | not measured before Phase 1 | **2** | ms | 5 | median (2–4) | same | 50 | yes |

### Three of these numbers are not comparable, and the temptation is the whole risk

* **`search_repo` vs `retrieve_context`.** T1's headline `0.648 s` is
  `harness/retrieval.search_repo`. The `19 380 ms` above is
  `harness/retrieval.retrieve_context` — the function the per-step path
  actually calls. They are different functions and **the 366× walk win does not
  transfer between them.** Quoting `0.648 s` next to "fast enough to use daily"
  would be the single easiest over-claim in this phase, and it is the one this
  document exists to prevent.
* **`cold` is not `warm`.** The cold figure is reported alone. The warm pass is
  in the ladder report (`warm_median_ms`, `warm_window`) and is never averaged
  with the cold number.
* **Host-dependent.** Every figure is one checkout on one machine under four
  concurrent terminals. It is a claim about that, not about the product.

### The one number that IS a mechanism, not a timing

`evals/rung_break_demos.py::demo_rung9_second_skip_list` restores the second
skip table and re-measures the same walk on the same checkout:

| arm | directories opened | walk time | object identity across the 3 harness walks |
|---|---|---|---|
| shared authority (44 names) | **164** | 87 ms | `True` |
| private table, no `logs` (14 names) | **≥ 60 000** (hit the counting cap) | 33 084 ms | `False` |

**366×**, and the count is a floor because the walk stopped at the 60 000 cap.
This is `DOCTRINE.md` §8's 345× signature, reproduced on demand. It is a
*mechanism* assertion: it does not depend on the clock being quiet.

---

## 2. The 2× cost — in this document, not in a footnote

> **THE COST OF PHASE 1, STATED WHERE THE SPEED WINS ARE STATED.**
>
> The per-task turn ceiling went **25 → 60**, a **2.4×** increase in the
> **worst-case** number of model turns in one task. A ceiling is not a bill:
> most tasks finish far inside it, and the binding limits on spend are
> `budget_cap_usd` and `max_wallclock_s`, enforced independently of the turn
> count.
>
> **Typical case: MEASURED, and it is UNCHANGED.** `runtime/turn_cost.py`'s
> `MEASURED_TYPICAL`, from 502 real model calls across 50 completed tasks
> (`logs/ablations/{v4,ir2-final}/tasklogs/*/model_ledger.jsonl`):
> **calls per task — median 9, p95 18, max 22**; cost per task — median
> **$0.0102**, mean $0.0159, p95 $0.0425, max $0.0543.
> **Every task in that corpus finished below even the OLD 25-turn ceiling.** So
> the doubling costs **nothing** on any task anybody has actually run; it moves
> the tail, not the bill. T3's own caveat travels with the number: n=50 on
> fixture and synthesized bug classes, free-tier BYO routers priced at published
> comparable-model rates, one machine, one evening each — a **directional**
> typical case for this project's task shape, not a benchmark-grade cost.

### The per-tier worst case, measured

Default worst-case shape (T3's): **17 000 prompt + 800 completion tokens per
turn**; `budget_cap_usd` = **$2.00**.

| tier | model | $/M in,out | worst case @25 turns | worst case @60 turns | what BINDS | turns to reach $2.00 |
|---|---|---|---|---|---|---|
| easy | `gpt-4o-mini` | 0.15 / 0.60 | **$0.0757** | **$0.1818** (2.4×) | the **turn ceiling** | 660 — the $2.00 cap is NOT reachable |
| medium | `gpt-4o-mini` | 0.15 / 0.60 | **$0.0757** | **$0.1818** (2.4×) | the **turn ceiling** | 660 — the $2.00 cap is NOT reachable |
| hard | `claude-3-5-sonnet-20241022` | 3.00 / 15.00 | **$1.5750** | **$3.7800** (2.4×) | **`budget_cap_usd`** | **31** — below the new 60 ceiling |

Two consequences worth more than the 2.4× headline:

* **On the expensive tier the doubling changes nothing.** `budget_cap_usd` was
  already the binding guard at **31 turns**, which is *below* the new ceiling.
  A task on `hard` cannot reach 60 turns without having been stopped by the
  dollar cap first. The 2.4× is a headline that only the cheap tiers can
  actually spend, and there $2.00 is still 660 turns away.
* **The number is load-bearing only where the ceiling was the guard.** That is
  the opposite of the intuition ("we doubled the cap, so we doubled the bill"),
  and it is why the cost belongs next to the speed wins rather than in a
  footnote: a reader who has only the 2.4× will over-estimate the damage by a
  factor of ~21 on the default path.

### A third authority for the ceiling number — found while measuring this

`runtime/turn_cost.py` declares `TURN_CEILING_NEW = 50`. The shipping config
says **60** and `harness.turn_caps.resolve_caps()` resolves **60**. So the module
that computes the *cost* of the ceiling has the wrong ceiling in it, and its
2.4× is really 2.0×. This is the same defect class as the stale `__pycache__`
(§7.1): **one number, several readers, no cross-check.** Owner **T3**.

Neither the cost nor the speed is comparable to the other. A reader who quotes
the speed without the cost is quoting half the phase.

The ceiling figure is **computed from the shipping config at render time**
(`harness.config.DEFAULTS['agent_max_turns']`) and cross-checked against the
number the Trust Ladder independently read. Both read **60**. That cross-check
is a gate row (`turn_ceiling_cost_published`) and it **fails** if the two ever
disagree — see §7.

---

## 3. What was blocked, and why — exact errors

| row | status | exact reason |
|---|---|---|
| `live_provider_model_quality` | `blocked` | No live provider was reachable. T3 recorded `ServiceUnavailableError: No available channel` on **3/3** live completions. This gate re-attempted nothing and inspected **no credential**, so it makes **no** model-quality, model-cost or model-latency claim. Every model call in every rung and every lane is a **scripted double**. |
| `windows_lane` | `blocked` | The gate ran on Windows as a **host**, so the failures only a CI runner sees (path separators, newline translation, symlink privilege, `os.replace` semantics) were never exercised. The lane's **workflow** was checked for consistency (`ci_lanes_consistent` = pass); the lane's **run** was not performed and is not claimed. |
| Docker | **available** | `docker info` → `Server Version: 28.5.1; Storage Driver: overlayfs`. The daemon was reachable, so the Docker row is `pass`. Whether each Docker-backed lane was *run* is its own row — a reachable daemon does not launder an unrun lane. |
| rungs #1, #3, #4, #5, #6 | `blocked` (in the ladder) | No published `logs/gates/g0_report.json` carries their rows. The remedy is in each row. Reported `blocked`, never `pass`, never zero. |
| rung #7 | `not_implemented` | No gate measures cross-session recall. The P3a long-session eval does not exist. |

---

## 4. The scripted-model caveat, stated plainly

> **Every model call in every rung and every lane is a scripted double. No
> number in this document is a claim about model quality, model cost, or model
> latency.** What was measured is the **shell**, the **retrieval path** and the
> **projection chain**.

Concretely: `evals/trust_ladder_rungs.ScriptedModel` and
`evals/run.py`'s scripted boundary. `python -m evals.run --check` reporting
14/14 CLEAN is a **host self-check of a 14-task prompt set** — it proves the
eval harness is internally consistent and that no prompt change regressed a
scripted arm. It is **not** a model-quality gate, and no row in this report
treats it as one.

---

## 5. What G1 does not establish

1. **Model quality, model cost, model latency.** §4. No live provider.
2. **`--check` is a prompt-set self-check**, not a quality measurement.
3. **Rung #9's retrieval number is one checkout on one host.** A timing
   assertion in a *blocking* lane is the one thing this gate refuses to ship: a
   flaky timing test in a blocking lane trains people to ignore the gate, which
   destroys the gate's value.
4. **No p95 anywhere**, because no window reached 20 samples. The rule is in the
   type (`Timing.named_stat`), not in prose.
5. **The daily-driver readiness gate is not green and is not counted here.** It
   reports `NOT_READY`, and the capability
   `cost_latency_user_intervention_at_least_90pct` is **still `UNMEASURED`**.
   Unmeasured is `not_implemented` with an owner — it is not a pass, and it is
   not a row that might quietly turn green. Owner: **T5 / P1.4**.
6. **A green row is a statement about this tree, on this host, at this commit.**
   No reproducible build, no clean-room install, no tag.
7. **Phase 1's LOC delta is not recoverable from git history** — see §6.

---

## 6. LOC delta

Deletion is a feature, so both directions are reported. Two numbers, because
one honest number does not exist:

| what it measures | added | removed |
|---|---|---|
| **Phase 1 Wave 1, T4 (`cli/`)** — commit range `6287cb1..5ca97a7`, the only clean Phase 1 range in history | **+45 044** | **−1 906** |
| **Phase 1 Wave 2, all five terminals** — working tree vs `HEAD` at report time (tracked files) | **+12 772** | **−150** |

**Why there is no single "Phase 1" number.** T1/T2/T3's Wave 1 landed inside
commit `0b59a5c` ("Vex -> Neo rename, neo-agent-cli distribution, site on
neo-agent.si"), which **also** renames the distribution and adds a marketing
site. That commit measures **+450 011 / −16 777**, and none of those three
things is separable inside it. Reporting it as the Phase 1 delta would be a
number about a rename. The two rows above are what can actually be attributed,
and each is labelled by what it measures.

The Wave 2 deletion figure (−150) is small, and that is worth saying plainly
rather than dressing: **Phase 1 added far more than it removed.** The one place
deletion was the *point* is recorded in T1's own handoff — four hard-coded
turn-cap literals were **deleted rather than updated**, because a literal that
still exists is a literal that can drift. The repository is larger after Phase
1 than before it.

---

## 7. Two findings that no prompt anticipated

### 7.1 A stale `__pycache__` made a Phase 1 headline number wrong for a whole session

`harness/config.py` carries `"agent_max_turns": 60`. It also carried `25` until
a concurrent terminal rewrote it during Wave 2. **Both literals are two
characters long**, so the edit did not change the file's byte size, and it
landed inside the same filesystem-mtime granularity second. CPython decides
whether `__pycache__/config.cpython-310.pyc` is still valid by comparing
`(source mtime, source size)` — so it matched, it did **not** recompile, and
every process in this repository executed the **old** bytecode.

Measured, on this host, with the stale `.pyc` in place:

```
python -c "import harness.config as c; print(c.DEFAULTS['agent_max_turns'])"
25                       # what the product actually RAN
# and compiling the very same file from source, with no cache involved:
60                       # what the source SAYS
```

The Trust Ladder reads this exact key through
`evals.trust_ladder_rungs.declared_turn_cap`, so **rung #8's ceiling check was
reading the stale number too**. Every Phase 1 ceiling measurement taken in that
window was wrong, and the error is in the direction of the *older* value — which
is the worst direction, because it makes a phase look like it never happened.

Fixed and pinned: the caches were purged (404 `__pycache__` directories) and
`tests/test_config_defaults_match_source.py` (8 tests) now reads every key of
`DEFAULTS` **twice** — once by importing it, once by parsing the source with
`ast` (no import, no bytecode in the path) — and requires them to agree, with
the five spend-related keys pinned by name. The pin is what makes the next
occurrence visible instead of measured.

### 7.2 The rung #9 phase attribution was reporting a missing measurement as a negative

`attribute_retrieval()` runs **after** the cold retrieval has already built the
persistent code-graph index, so the call it profiles is a **warm** one. On this
checkout that call performs no phase the probe can name, the probe returned
`{}`, and the rung then rendered that as:

> *"NO single phase dominates, so no single-phase cause is claimed"*

which reads as a **measured** absence of a cause. It is a **missing**
measurement. The same function, run first in a fresh interpreter, returned
`tree_sitter_parse` at **88.3 %** — both numbers are true and they describe two
different calls.

Fixed: the attribution now carries `attribution_status`
(`measured` | `inconclusive`) and `explains_the_cold_number`, and an empty
attribution renders as **"INCONCLUSIVE — not negative"** with the reason and the
remediation (attribute the cold call itself, against an isolated `index_root`,
which this probe does not do because a fresh full index of this repository was
measured at **over 600 s**). **No cause is claimed for the 19.4 s.**

---

## 8. The full-suite result, adjudicated

`python -m pytest tests/ -q -p no:randomly` → **23 failed, 7342 passed, 33
skipped, 1 warning in 6153.33 s (1:42:33)**.

Each failure is named, owned, and classified. **None is reported as `pass`,
none is reported as `skip`.**

| # | node | classification | owner |
|---|---|---|---|
| 1-2 | `test_aesthetic_gate.py::test_no_hex_literal_outside_the_token_table`, `::test_the_whole_verdict_is_green` | **fail** — literal colours outside the token table (`cli/ui.py:1318`: `#7A0C26 #E8114A #FF7A93 #FFF0F3`) | T4 (`cli/`) |
| 3 | `test_aesthetic_gate.py::test_each_region_keeps_the_same_edge_in_every_receipt`, `::test_the_wrapped_continuations_are_measured_counted_and_owned`, `::test_all_four_type_roles_are_realised_on_every_composition_receipt` | **fail** — in-flight during the run; same family as 1-2 | T4 (`cli/`) |
| 4 | `test_ceiling03_sessions.py::test_five_thousand_indexed_sessions_list_under_100ms_p95` | **HOST-DEPENDENT TIMING.** Failed in the full-suite run, passed standalone; its own diagnostic reported the host 1.89× slowed. This is exactly why it lives in non-blocking `host-only` and not in a blocking lane. | T5 (lane placement), T4 (the test) |
| 5 | `test_ceiling16_surfaces.py::TestUninstallPreservesUserData::test_the_plan_never_targets_user_data` | **fail** — the uninstall plan touches user data | T4 (`cli/uninstall.py`) |
| 6-7 | `test_cli.py::test_run_benchmark_smoke_offline`, `::test_run_benchmark_json_subset` | **fail** — offline benchmark assertions | T4 (`cli/`) |
| 8 | `test_cli_connectors.py::test_duplicate_plugin_labels_keep_winning_source` | **fail** — `plugin.json requires BOTH 'name' and 'version'`; the fixture and the loader disagree | T4 (`cli/plugins.py`) |
| 9 | `test_cli_plugins.py::test_slash_unknown_lists_available_customs` | **fail** — `/deploy` absent from the unknown-command listing | T4 (`cli/`) |
| 10-11 | `test_cli_theme.py::test_no_literal_color_outside_the_token_authority`, `::test_every_literal_color_exemption_is_still_used` | **fail** — same literal-colour family as 1-2; the second says an exemption at `cli/ui.py:1317` is now stale | T4 (`cli/`) |
| 12-13 | `test_cli_tui.py::TestTraceCommand::test_trace_lists_and_expands`, `::test_trace_after_run_rebuilds_from_trace_file` | **fail** — the `/trace` surface | T4 (`cli/tui.py`) |
| 14-15 | `test_command_args.py::TestExistingTemplatesUnchanged::test_every_command_template_in_the_tree_matches_fill_template`, `::test_the_project_command_and_the_plugin_command_are_both_covered` | **fail** — a fixture command template (`plugin-webapp-toolkit/commands/review.md`) drifted | T4 (`cli/command_args.py`) |
| 16 | `test_config_trace_state.py::test_get_config_handles_none` | **ORDER-DEPENDENT**, not a regression: passed standalone, failed inside the full run. `shared/AGENTS.md` records the cause — `cli/commands.py::apply_effort` leaks `NEO_EFFORT` into the process environment, and this test is the victim. | T4 (the leak), T5 (the victim) |
| 17 | `test_context_budget_engine.py::test_killed_run_resumes_with_its_context_budget_and_prior_turns` | **fail** — a real subprocess hard-kill resume; needs attribution | T1 (`harness/`) |
| 18-19 | `test_daily_session_human.py::TestAVisualRegressionBecomesADiff::test_the_same_tree_typed_twice_renders_the_same_text`, `::test_the_corpus_survives_a_repeat_typing` | **fail** — a visual-regression determinism pair; the whole point of that file is that these two agree, and they did not | T4 (`cli/tui.py`) |
| 20-21 | `test_module_reachability.py::test_every_production_module_has_a_production_importer`, `::test_no_recorded_exemption_may_be_stale` | **fail, and T5's to fix.** 38 modules have no production importer and are not recorded — and **36 of the 38 are the four terminals' own module-local `test_*.py` pin files**, invisible to `pytest tests/`. The other finding is a stale exemption (`extensions.skill_policy`). This row is why task W2.4 exists. | T5 |
| 22 | `test_scheduler_integration.py::TestWorkerSafety::test_worker_exception_output_redacts_credentials` | **fail — a real assertion drift.** The test expects `[REDACTED]`; the authority emits `[REDACTED_SECRET]`. The secret IS redacted; the **test hard-codes the old marker** instead of reading the authority's constant. Re-dacting is correct, so the test is what should change. | T5 (both files are mine) |
| 23-24 | `test_terminal_05_file_change.py::TestDiagnosticsPanel::test_the_diagnostics_panel_keeps_its_link_callback`, `::test_announce_is_called_with_its_key` | **fail** — the diagnostics panel's link callback | T4 (`cli/tui_components.py`) |

### Module-local pins (the four terminals' own files)

37 module-local modules, **851 passed, 5 failed, 1 error**:

* `execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub`
  — **the pinned unsandboxed-bash defect, still open.** Owner **T1 / P2.1**. It
  is the registry's only `KNOWN_FAILING_PINS` entry and it is the first item in
  the Phase 2 brief.
* `runtime/test_w2_pins.py` × 4 — in-flight T3 Wave-2 work (the preload race and
  the vision caps), not adjudicated as defects this round.
* `harness/test_config.py::test_config_guard` — **ERROR on collection**, the gap
  `harness/AGENTS.md` has recorded since P0/W2: a production module named
  `test_*.py` whose public function pytest collects as a test with two fixtures
  that do not exist. Owner T1 + T5.

---

## 9. The six G1 rows that are `fail`, and what closes each

| row | what is broken | owner | what closes it |
|---|---|---|---|
| `rung_09_measured` | cold `retrieve_context` is 19 380 ms against a 1000 ms budget. **Cause NOT claimed** (§7.2). | T1 | attribute the cold call, then fix whatever it names |
| `rung_10_measured` | 4 findings: LINK 3 drops `module`/`source`; a `retrieval_truncated` row reaches no fact and no card line; the card has no vocabulary for an absent value; an unpriced run renders `$0.000000` with no `unpriced`/`vacuous` marker | **T4** (`cli/runview.py`, `cli/tui_components.py`) | all four are `DOCTRINE.md` §1/§3 violations on a reader-facing surface |
| `full_suite_tests_dir` | 23 failures, adjudicated in §8 | 22 × T4, 1 × T5, 1 × T1 | see §8 |
| `module_local_pins` | 5 failed + 1 collection error across the terminals' own pin files | T1/T2/T3/T4 | see §8 |
| `binary_digests_pinned` | ripgrep and `fd` are fetched at run time with a **version** pinned in code, but their **SHA-256 digests are read from an upstream sidecar at download time and are not committed here.** The expected digest therefore travels with the payload, so a replaced asset with a replaced sidecar is accepted. | **T1 / P2.1 + T5** | commit the eight digests under `committed_digests` in `provenance/binaries.json` |
| `single_skip_authority` | **7 independent skip-set tables**, 6 adopters. Five prune *less* than the authority — `DOCTRINE.md` §8's exact defect. | **T2 + T3 + T4** (adoption), T5 (the audit + pin) | import `shared/skipset`; the count is pinned, so a table appearing fails and a table disappearing asks you to delete its entry |

---

## 10. What passed, and why it is not self-congratulatory

| row | evidence |
|---|---|
| `rung_checks_demonstrated` | **5/5** deliberate breaks constructed; each rung's own `derive_failures()` fired **and stopped firing** when the mechanism was restored; every file restored, digests matching. §11. |
| `rung_08_measured` | 56 turns, 55 distinct real edits, cap 60 declared **and announced**, and the turn-2 constraint still governing at turn 45 (visible on 56 turns). **T1 fixed the constraint decay during Wave 2, live** — the first ladder run in this phase measured it red. |
| `eval_prompt_self_check` | 14/14, verdict CLEAN — a prompt-set self-check, not a quality claim (§4). |
| `ci_lanes_consistent` | 6 lanes, 4 blocking, 2 non-blocking timing lanes, registry gate in **every** lane, no workflow disagreement. |
| `known_failing_registry_gates` | exit 0; 5 registered pins, 0 build failures; a fired pin classifies as `changed_reason`/REGRESSION and a closed gap as **`promote_me`** with the owner and the exact closure condition (§11.4). |
| `provenance_clean` | 0 undeclared, 0 incomplete, 0 mismatched. |
| `docker_daemon_reachable` | `Server Version: 28.5.1; Storage Driver: overlayfs`. |
| `config_defaults_match_source` | every `DEFAULTS` key agrees with the file, five spend keys pinned by name (§7.1). |

---

## 11. The five rung-break demonstrations

`python -m evals.rung_break_demos` — `RUNG_CHECKS_DEMONSTRATED`, 5/5. A rung
whose check has never been seen to fire is decoration, so this is a **row** in
the gate, not a transcript.

### 11.1 rung #8 — the turn ceiling set back to 25

```
SHIPPING arm: harness.config.DEFAULTS['agent_max_turns'] = 60;
              declared_turn_cap() read 60; cap finding = none
BROKEN   arm: declared_turn_cap() read 25 from the substituted config
              (DEFAULTS still reads 60 through the original module object,
              which is how the substitution is known to be scoped)
derive_failures() -> the declared turn ceiling is 25 at
              harness.config.DEFAULTS['agent_max_turns']; the guarantee needs >= 50
```
Both arms run in **one** child process, back to back: an earlier version ran
them as two processes and the "shipping" arm read 25 while the tree's own
literal said 60, because a concurrent terminal was editing that file. A
comparison whose halves come from different instants is not a control.

### 11.2 rung #8 — the constraint re-injection removed

```
ARM `removed`  : visible_turns=1/56,  visible_at_check=False, forbidden-path writes=53
ARM `present`  : visible_turns=56/56, visible_at_check=True,  forbidden-path writes=0
removed -> CONSTRAINT DECAY finding present: True
present -> CONSTRAINT DECAY finding present: False
```
Two arms, deliberately: a check that fires on a broken tree **and on a repaired
one** is a constant, not a measurement.

**The mechanism was found by measurement, and it was not the obvious one.** The
first version removed `DailyCodingStrategy._reinjection` — which gates on
`context_reinjection_fraction` — and **both arms came back with the constraint
visible on every turn**. The block that actually governs is
`harness/agent_loop_step.py::_constraint_block`, rendered as
`[CONSTRAINTS - still in force]` on every tool result. A break aimed at the
wrong function is not a weak demonstration, it is no demonstration, and it is
indistinguishable from a check that cannot fire unless you also run the control
arm.

### 11.3 rung #9 — the second skip table restored

```
AUTHORITY arm  : the shared set carries 45 directory names; the walk opened
                 164 dirs in 87.3ms; object identity across
                 retrieval/agent_loop/scan_mode = True
SECOND-TABLE arm: the private set carries 14 names and omits `logs`
                 (present: False); the walk opened 60000 dirs in 33083.9ms
                 = 366x the authority arm; object identity = False
the count hit the 60,000-directory ceiling (so the true count is higher): True
```

### 11.4 rung #9 — the provider import eager again

```
BEFORE (no eager provider import): 407.4ms (median over 3 runs, min 392.7 max 414.2)
BREAK  (module-scope `import litellm`): 9335.5ms (median over 3 runs, min 9200 max 9737.1)
budget: 2000.0ms
derive_failures() -> startup_to_first_token: 9336ms ... exceeds its budget of 2000ms
file outcome: restored -- the demo wrote its own bytes back and the digest matches
```
`measure_startup` times a **fresh interpreter**, which is the only shape that
can see this: in-process, `cli.interactive` is already imported and the number
would be a dictionary lookup. That is why this probe spawns a subprocess at all.

**This is the only demonstration in the file that writes to another terminal's
source, and it is guarded.** In a tree four terminals are writing, a
demonstration that reads a peer's file, mutates it, and writes a stale snapshot
back **deletes whatever the peer wrote in between** — which is exactly what the
first version of this module did, and its digest check firing is what revealed
it. The restore is now conditional and reports three outcomes: `restored`,
`superseded` (a peer wrote after us; their content stands, nothing is written
back), `restore_refused`. The other four demonstrations need no file write at
all.

### 11.5 rung #10 — one link of the chain dropped

```
ARM drop_card      : links present = journal_row=True, traceview_reconstruct=True,
                     runview_projection=True, tui_card=False; card rendered 1 chars
ARM drop_card      : derive_failures() -> LINK 4 (TUI card): the card rendered
                     nothing on a real run that edited two files.
ARM drop_traceview : links present = journal_row=True, traceview_reconstruct=False,
                     runview_projection=True, tui_card=True
ARM drop_traceview : derive_failures() -> the chain is broken: LINK 2
                     (traceview_reconstruct) produced nothing, so nothing
                     downstream can be trusted. | HONEST PRESENTATION (truncated): ...
findings naming a missing link: drop_card -> 1, drop_traceview -> 1
```
Two arms because the link that is **already broken** in the shipping tree is
LINK 3 — a check demonstrated only against an already-red tree proves nothing.

### 11.6 And the registry's own promote-this path

```
node   : execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_no_production_
         import_path_reaches_the_local_subprocess_sandbox_stub
owner  : T1 / P2.1
verdict: promote_me
detail : PROMOTE THIS. The recorded gap is CLOSED: the pin passes. Owner was
         T1 / P2.1; it closes when harness/agent_loop.py deletes the
         `from harness._stubs import sandbox` reach and the local-subprocess
         execute_sandboxed call that follows it. Nothing else in the repository
         changes.. Delete the entry from KNOWN_FAILING_PINS in the same change
         that closed the gap, so the closure is visible in the diff.
```
---

# G2 — the Phase 2 Trusted Execution verdict

**Verdict: `G2_RED`.** Seven rows pass, **three** are `fail`, **two** are
`blocked`, zero are `not_implemented`. No row is `skip`; `skip` is not a member
of the vocabulary this section is written in, and `evals/gates/P2.py` imports
the vocabulary from `evals/gates/P0` rather than re-declaring it, because two
lists is how `skip` gets added back.

**Two rows moved after this section was first written, and both moves are part
of the record rather than an edit to it.** Rung #5 was `fail` (the provenance
spine was RED) and is now `pass` — T1 reconciled the header vocabulary mid-round.
Rung #10 was `blocked` (no full-suite log) and is now a measured `fail` — the
suite ran to completion in 1:23:23. A trust report that quietly re-renders its
own history is not a trust report.

Machine-readable source of every number below:

```
python -m evals.gates.P2            # rendered report, exits 2 while red
python -m evals.gates.P2 --json     # the same report, machine-readable
python -m evals.gates.P2 --selftest # the gate's own can-it-fail proof
```

**Machine:** `Windows AMD64 / cpus=12 / python 3.10.11 / Docker 28.5.1
reachable`.

**What `G2_RED` means.** At least one *blocking* trust guarantee was
**measured** and did not hold, or could not be exercised at all. It is a
published measurement, not a failed build. No row was relaxed, no budget was
widened, and no CI lane was edited to make a row pass.

---

## 0. The headline deliverable: the unsandboxed-bash defect is CLOSED

This was the phase's one pinned known-failing test and the whole point of the
"subtractive" framing. The closure is a **two-part event** and both parts are
done.

**Part 1 — the defect is gone, proven by running the pins.**

```
$ python -m pytest execution/test_unsandboxed_pins.py execution/test_p2w1_trust_pins.py -q
75 passed, 6 warnings in 30.17s
```

`harness/agent_loop.py` no longer imports `harness._stubs.sandbox` and no longer
calls the local-subprocess `execute_sandboxed`. The only remaining occurrences
of `_stubs` in that file are explanatory comments, at lines **664, 668, 681,
2204 and 2234** — measured by grep, and each one is prose about the boundary
rather than a reach to it.

**Part 2 — the registry entry was deleted in the same change**, which is what
makes the closure visible in the diff:

```
$ python -m pytest tests/test_known_failing_pins.py -q
18 passed in 32.84s

KNOWN_FAILING_PINS            = ()          # was: T2's unsandboxed-bash pin
KNOWN_FAILING_PINS_DECLARED_EMPTY = True    # the emptiness is DECLARED
INVERTED_PINS                 = 6            # was 4; Phase 3's two added
```

The emptiness is a **named value, not a comment**, and that is a deliberate
change to the meta-test's contract. Its previous assertion was
`assert KNOWN_FAILING_PINS`, whose own failure message asked for "a comment" —
and a comment cannot be asserted on, so it could not distinguish a deliberately
emptied registry from one emptied by accident. It now accepts an empty
registry **only** in the declared form, and additionally requires
`INVERTED_PINS` to be non-empty in that state, because "no known gaps" with no
evidence either way is a different claim.

## 1. Rungs #1–#6, each with its named proving test

| rung | status | what was measured |
|---|---|---|
| **#1** unsandboxed-bash path closed | `pass` | 75 pins pass; the defect is gone and the registry entry is deleted. |
| **#2** every untrusted source scanned | `pass` | 7 sources declared; **removing** `review_untrusted_source` moved **15 of 15** canary cases from blocked to unblocked. |
| **#3** policy language fails closed | `pass` | 3 broken rules each refused naming the rule + example; 29 shipped rules carry **75** examples with **0** failing and **0** rule shipping none; reordering four layer orders changes the answer. |
| **#4** corpus is non-vacuous | `pass` | 67 cases across 6 groups, **every count pinned EXACTLY**; every Phase-2 surface non-zero; each defence removed in turn moved cases (below). |
| **#5** vendored provenance declared | **`fail`** | The spine is RED. Three files carry a header the scanner cannot parse, and `provenance/` is gitignored. |
| **#6** verifier mint unchanged | **`fail`** | The pin passes, but it records a gap: the doctrine says one mint site and the tree pins **3**. |

Plus six rungs outside the numbered six:

| rung | status | why |
|---|---|---|
| #7 hardline floor reaches an interpreter | **`blocked`** | `runtime.hardline.screen_or_raise` has **no production call site**. |
| #8 fetched binaries carry committed digests | **`fail`** | ripgrep 14.1.1 and fd 10.2.0 have versions pinned and **no committed SHA-256**. |
| #9 live provider exercised | **`blocked`** | Never run. Every model call in every suite is a scripted double. |
| #10 full `tests/` tree adjudicated | **`fail`** | **101 failed, 7 493 passed, 33 skipped in 1:23:23.** Dominant class: `ValueError: unknown agent strategy: legacy_agent`. |
| #11 registry gate | `pass` | 18 passed. |
| #12 Docker daemon | `pass` | Reachable, 28.5.1. |

## 2. The corpus, and how non-vacuity was proven

**The count is pinned EXACTLY**, which closes the `>=` assertions that let three
declared threat-model categories sit at zero cases while reading as satisfied.

| group | cases |
|---|---:|
| `SECURITY_REGRESSION_CORPUS` | 23 |
| `CANARY_EXFILTRATION_CORPUS` | 7 |
| `OBFUSCATED_CANARY_EXFILTRATION_CORPUS` | 8 |
| `ISSUE_INJECTION_CORPUS` | 9 |
| `DELIMITER_BREAKOUT_CORPUS` | 7 |
| `PHASE2_SURFACE_CORPUS` | 13 |
| **total** | **67** |

Per-surface coverage, a zero reported as a zero: `hardline` 6, `fuzzy_edit` 2,
`approval_gate` 2, `approval_origin` 1, `containment_receipt` 1, `vendored` 1.

**Non-vacuity, by removing each defence and re-probing.** Reproduce with
`python Temp/opencode/w2_1_nonvacuity.py`.

| defence removed | cases that moved | direction |
|---|---:|---|
| `shared.security.review_untrusted_source` | **15** | blocked → unblocked |
| `shared.security.defang_untrusted_delimiters` | **5** | clean → boundary literal survives |
| `shared.security.redact_text` | **2** | a canary leaks into the output |
| `shared.security.normalize_for_redaction` | **1** | a canary leaks into the output |

Every pass restored the exact prior dispositions, so the A/B is real and not a
one-way measurement.

**The finding this produced, and it is the one worth reading.**
`shared/security_corpus.py`'s own header asserts that every obfuscated canary is
framed so that "a strict body class is the **only** defence", and that a case
asserting normalisation "saved" the secret "would assert something false".
**Measured: normalisation is load-bearing for exactly 1 of 8 rows.** The other
seven are defended by rules whose character class spans the inserted character
anyway. So normalisation is real and it is load-bearing — but the corpus's own
stated mechanism is overstated, and `shared/AGENTS.md`'s "5 of 7 cases are
load-bearing" is now 1 of 8. The count is pinned as a *measurement with a
stated band* (`>= 1`, `<= 3`) rather than a promise, so it moves honestly.

**Uncovered classes, named explicitly rather than left to be inferred:**

* `containment_receipt_forgery` — declared as a corpus case and modelled in
  `shared/approval.py`, but **no production path mints a containment receipt a
  task could forge**, so the case documents a class it cannot yet fail closed
  on.
* `vendored_file_tamper` — declared as a case, but
  `scripts/provenance_report.py` enforces provenance at the **directory** level,
  not per vendored file, because no vendored file carries the `# vendored:`
  header the scanner parses. See §4.

## 3. The policy language proves itself by refusing to load

**Three deliberately broken rules, three refusals**, each naming the rule and
the failing example. Reproduce with `python Temp/opencode/w2_2_transcripts.py`.

| broken rule | refusal message |
|---|---|
| `match` example does not match | `rule 'w2_broken_match' (layer 'defaults') example 'command=git' does not behave as declared: expected 'deny' and for this rule to be the deciding rule, got 'ask' decided by 'layer_default'` |
| `not_match` example DOES match | `rule 'w2_broken_not_match' (layer 'defaults') example 'tool=shell' does not behave as declared: the rule MATCHES this subject, so it cannot be listed as one it does not apply to` |
| malformed decision | `unsupported policy decision 'forbiddenish'; expected one of ['allow', 'ask', 'block', 'deny', 'forbidden', 'permit', 'prompt'] (rule 'w2_bad_decision')` |

A fourth, which is the floor's own invariant: a `floor` rule carrying `allow`
is refused — *"the floor layer only carries denials"*.

**Two of my first three transcripts were refused for the WRONG reason**, and
that is recorded because it is the trap this whole round is about. `command=git
status` was refused by the example **grammar** for a bare token, and
`command=rm -rf /tmp/other` likewise — a refusal that points the reader at the
wrong defect. The shipped examples now parse, so the refusal is semantic.

**The shipped policy's examples, run as a suite: 29 rules, 75 examples, 0
failing, 0 rules shipping without an example.** A rule with zero examples is a
named finding, and there are none.

**Layering is order-dependent, which is what makes it a security property.**
The *same three rules* under four layer orders:

| order | answer |
|---|---|
| `defaults → user_config → per_session` (as documented) | `deny` by `per_session` |
| `per_session → user_config → defaults` | `ask` by `defaults` |
| `defaults → per_session → user_config` | `allow` by `user_config` |

Three distinct answers from three identical rules. And the floor beats all of
them in every order, with `floor=True` on the outcome.

**One row of that table is conditional, and it is recorded rather than smoothed
over.** During this phase the shipped policy **refused to load twice**, both
times on `floor:hardline:language_runtime_destructive_call`'s declared example
`command='ruby -e FileUtils.rm_rf "/"'`, because T3's
`hardline.screen_or_raise` returned `None` (allowed) for that exact command. The
floor's matchers for the *other* shapes (`ruby -e 'require "fileutils";
FileUtils.rm_rf("/")'`, `python -c "import shutil; shutil.rmtree('/')"`) refuse
correctly. **This is the mechanism working and a real defect at the same time**,
and it is the clearest single argument for the design: a policy language that
validates its own examples caught a rule whose declared proof was false, without
a human reading either file. The cause was a T3 table mid-edit; it resolved, and
the fold now reports **20 of 20 patterns folded, available=True**.

**A gate that crashes on a broken input is not a gate.** The first version of
`evals/gates/P2.py` called `shipped_policy()` directly and **died** with a
`PolicyValidationError` on its first run — the exact failure it exists to
catch, met with the worst possible response, because "no report" reads like "no
problem". `policy_measurement()` is now total and returns a refusal as a
*value*, and every rung derivation is wrapped by `_safe()`, which converts any
escape into a `fail` naming the traceback's last line.

## 4. The vendored-code inventory

| file | upstream | licence | byte copy? | sha256 |
|---|---|---|---|---|
| `harness/third_party/opencode_replacer.py` | https://github.com/sst/opencode | **MIT** | **No** — an independent reimplementation of the nine-stage cascade; the matching stages were ported, not the file | `d3087952ba47…` |
| `harness/third_party/aider_bitap.py` | https://github.com/Aider-AI/aider | **Apache-2.0** | **No** — an independent reimplementation of the Bitap matcher; one function, not the ~2,000-line file | `452658babd6b…` |

Checksums are **pinned and compared on every run** of
`tests/test_p2w2_vendored_proof.py` (24 tests), so a vendored file edited in
place is visible rather than silent. `NOTICE.md` is pinned too, because it
carries the Apache-2.0 attribution and its bytes are licence-visible.

**All four header fields are present, and the reason field is specific.** Both
modules carry `Upstream`, `Commit pin`, `SPDX-License-Identifier` and
`Why it beats ours` in the module docstring, and both reasons clear a
200-character floor (the gate's own floor is 15, which is a floor and not a
standard). Each docstring also states **what was NOT taken** — OpenCode's
*ordering* was deliberately inverted, `diff_match_patch`'s other ~2,000 lines
were not reproduced — so a future reader does not assume the file *is*
upstream's.

**Licence compatibility, recorded.** This repository is MIT. Both upstreams are
permissive and compatible. The one obligation picking MIT does **not** discharge
is Apache-2.0 §4, which is why `harness/third_party/NOTICE.md` exists and why
`aider_bitap.py` must never be re-declared `licence: MIT` — pinned, so a
future edit cannot make that mistake silently.

**Rung #5 is now `pass`, and the gate does NOT check what you would hope.**
T1 reconciled the header vocabulary during this phase: the vendored files now
carry the scanner's `# vendored:` block, and
`python scripts/provenance_report.py` exits **0** with
`declared: 3, undeclared: 0`.

**But `scripts/provenance_report.py` reads the header as TEXT and never
compiles the file.** While this section was being written, both vendored
modules were mid-edit with the new header block inserted **above** the module
docstring — which removes the docstring's opening triple-quote and leaves the
file unterminated. Measured at that moment:

```
harness/third_party/aider_bitap.py        SyntaxError line 10
harness/third_party/opencode_replacer.py  SyntaxError line 17
```

while `provenance_report.py` exited **0** with `declared: 3, undeclared: 0`.
**The gate went green on two files that could not be imported.** That is the
single most useful thing this audit found about the gate, and it is now pinned
by `test_every_declared_vendored_file_MUST_PARSE` in
`tests/test_p2w2_vendored_proof.py` — because the gate's own claim is that a
vendored file is *auditable*, and an unparseable one is auditable by nothing.

(The original finding — that the gate was red for a header **FORMAT** reason
rather than a licence problem — was correct when measured, and is recorded in
`INTERFACES.md` as the correction. The fix was a header, exactly as that entry
said it would be. It was not a licence change, and no licence changed.)

**Two further provenance findings, both pinned:**

* `provenance/binaries.json` is **gitignored** (`.gitignore:100`), so the
  binary manifest is a claim **no reviewer can see in a diff** — while
  `scripts/provenance_report.py:466` describes it as "a committed JSON
  document". Pinned so the contradiction cannot be forgotten.
* The tracked root `NOTICE` still reports `declared: 0, undeclared: 0` and
  still says "NOTHING FROM AIDER IS COPIED INTO THIS TREE". Both are false
  today. Pinned as a **recorded contradiction** so it is visible rather than
  believed.

**The binaries.** Both are declared with a version, a source URL and an honest
statement of what is pinned:

| binary | version | licence | digest committed? |
|---|---|---|---|
| ripgrep | 14.1.1 | **`MIT OR Unlicense`** — MIT relied on | **No** |
| fd | 10.2.0 | **`MIT OR Apache-2.0`** — MIT relied on | **No** |

Both licence facts were **corrected against the brief that introduced the
work**: ripgrep is dual-licensed, not "MIT". MIT is relied on in both cases
because it carries an attribution-and-notice obligation `NOTICE` can discharge.

**Rung #8 is `fail` and it is Phase 1's recorded open gap, still open.** The
`VERSION` of each binary is pinned in `harness/search_engine.py` and the
manifest agrees; the **SHA-256 is not committed anywhere in this repository**.
The downloader verifies against an upstream `.sha256` sidecar, so the expected
digest **travels with the payload** — a replaced asset with a replaced sidecar
is accepted. A checksum-verified download whose checksum is not committed is
the paperwork version of the same honesty problem.

## 5. The attack results — every row, including any that succeeded

**T2 (execution), 15 rows.** Sandbox runs one command per fresh container ·
repo bind-mounted read-write at `/workspace` · read-only rootfs with cap-drop
ALL · no network unless `allow_network=True` · `docker.sock`/`containerd.sock`
mount · host PID/IPC/network/user namespace · host secret-directory mount ·
mount outside the workspace · added capability · any security option but the
baseline · Windows drive letter read as a volume separator · traversal write
landing on the container rootfs · fuzzy landing on the protected test surface ·
containment receipt authored by a task · daemon stopped or unreachable.

**T3 (runtime), 15 rows.** Quote splice hiding the root path · env prefix ·
`sudo` prefix · command substitution · path traversal to a protected path ·
pipe to shell · base64/splice/substitution deobfuscation ·
`NEO_HARDLINE_ALLOW` exported before import · the same set after import ·
`importlib.reload` · module attribute reassignment · approval-gate injection
via command content · approval-gate injection via a filename · subagent origin
claiming the parent thread.

**Result: 30 of 30 refused, zero succeeded.** Every row is listed rather than
summarised, because a trust report that omits a failure is the thing this
repository exists to prevent.

**But the result needs its own honest caveat, and it is the most important
sentence in this section.** These rows are properties of the **tables**, and
**two of the guards have no production call site**:

* `runtime.hardline.screen_or_raise` is called from nowhere in `execution/` or
  `harness/`. T3 records this as not wired.
* `harness/approver.py` and `runtime/approver_gate.py` have **zero** production
  callers.

So "30 of 30 refused" means *the matcher refuses every shape it was given*. It
does not mean a running guard refused 30 attacks, because there is no running
guard. Rung #7 is `blocked` for exactly this reason.

## 6. LOC delta — both directions, because Phase 2 was subtractive

```
71 files changed, 20611 insertions(+), 2604 deletions(-)   net +18007
DELETED: harness/agent_kernel/legacy.py
```

Per module, tracked files only:

| module | files | insertions | deletions |
|---|---:|---:|---:|
| `harness/` | 23 | 3,865 | **1,868** |
| `runtime/` | 10 | 5,731 | 43 |
| `execution/` | 10 | 3,954 | 83 |
| `cli/` | 10 | 2,942 | 287 |
| `shared/` | 4 | 1,465 | 2 |
| `evals/` | 3 | 1,185 | 36 |
| `memory/` | 2 | 77 | 24 |

**Two honest qualifiers on those numbers.** First, they cover **tracked files
only** — this wave's new modules are **untracked** in a shared worktree, so the
insertions are an **understatement**. Second, the deletion is the deliverable:
`harness/` alone lost **1,868 lines** and `harness/agent_kernel/legacy.py` is
the one deleted file in the whole Phase 2 diff. A reader shown only the
insertion count would conclude this was a growth phase, and it was not.

## 7. What is still `blocked`, with the exact reason

* **Live model provider — `blocked`.** No live-provider lane was run and none
  is claimed. No credential was inspected, requested or retained. **Every model
  call in every suite is a scripted double** through the documented
  `harness.deps.set_call_model` seam or the offline mock provider. **No result
  in this document is a claim about model quality** — including the model that
  would be needed to *exploit* the injection surfaces this gate covers, which is
  the sharpest version of that caveat: this phase measures the defences, not the
  attacker's capability to use them.
* **Full `tests/` tree — MEASURED, and it is RED.**
  `python -m pytest tests/ -q -p no:randomly` -> **101 failed, 7,493 passed,
  33 skipped in 5003.01 s (1:23:23)**, 0 collection errors.

  **The dominant failure class is `ValueError: unknown agent strategy:
  legacy_agent`** — 28 in `test_agent_loop.py`, 16 in
  `test_agt_02_reflection.py`, 10 in `test_agt_10_batch_boundary.py`, i.e.
  **54 of the 101**. T1's engine collapse deleted `legacy_agent`, and the
  suites that pin it *by name* were never repinned.

  **That is T5's own debt, not a product defect**, because `tests/` is this
  terminal's directory, and this round fixed exactly one file of it
  (`tests/test_ceiling_r2_04_daily_default.py`, which had been blocking
  collection of the ENTIRE suite). The remaining 53 are a mechanical sweep and
  are the first thing the next T5 round should close. They are reported here
  rather than omitted because a trust report that hides 54 red tests is the
  thing this repository exists to prevent.

  The remaining 47 failures are spread across `test_cli_tui.py` (6),
  `test_aesthetic_gate.py` (6), `test_command_types.py` (5),
  `test_cli_tui_layout.py` (5), `test_trust_ladder.py` (3), and ~20 files with
  one or two each. **None is adjudicated as pre-existing in this document**,
  because adjudicating 101 failures was not done and guessing would be worse
  than saying so.
* **Hardline floor wiring — `blocked`.** No production call site (rung #7).
* **Docker — NOT blocked.** The daemon answered (28.5.1). This is the one
  Phase-2 precondition that is green, and it is reported as green precisely
  because the other three are not.

## 8. The honest containment limits — reproduced, not summarised

These are T2's and T3's own words-as-written, carried forward verbatim because
a paraphrased containment limit is a weaker one:

1. **Kernel escape.** Docker's isolation is the boundary; a container escape is
   a HOST compromise and nothing here detects or limits it.
2. **A compromised Docker daemon.** Every containment claim assumes the daemon
   is honest. Root on the host owns every container.
3. **A platform-ignored mount.** Docker Desktop / WSL2 can honour a mount
   differently from a Linux daemon; a mount measured refused on one host may not
   be refused on another. **No cross-platform differential is run.**
4. **A compromised dependency.** litellm, tree-sitter and every wheel in the
   image run with the harness's privileges. Only two fetched binaries have a
   declared source; **neither has a committed digest**.
5. **Container egress is NOT filtered.** Docker has no portable per-container
   egress allowlist without an external proxy, so `allow_network=True` attaches
   the default bridge. The **host-side** fetch path is enforced.
6. **The floor is not wired.** A guard nobody calls cannot refuse anything.
7. **The aux-LLM approval gate is not wired.**
8. **No model-quality claim.**

## 9. What this gate does not establish

* MEASURED on this tree, on this host, at this commit, with a scripted model.
  Re-run before quoting any number.
* **The whole tree was a SHARED DIRTY WORKTREE during this phase.** Four
  terminals were editing it concurrently, and **several of the numbers in this
  document moved while they were being taken**: `runtime/hardline.py` changed
  from 16 to 20 patterns and back, the hardline fold went 16/16 → 0/20 → 19/20
  → 20/20, the shipped policy went 28 rules/72 examples → did-not-load → 9
  rules/34 examples → 29 rules/75 examples, and T3's suite went 24 failed → 4
  failed. That is recorded rather than smoothed over, and it is why several
  rows here are measured continuously rather than pinned once.
* The gate certifies **measured claims about this tree**, not a release, and not
  a build.
* `python -m evals.gates.P2 --selftest` is the can-it-fail proof: it checks that
  a pass is blocking by default, that `skip` **cannot be constructed**, that
  `blocked()` refuses an empty reason, that `not_implemented()` refuses an empty
  owner, that one blocking `fail` turns the verdict red, and that a `blocked`
  rung keeps the gate red rather than being read as a pass.