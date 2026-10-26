# G3b — the tools ablation number, and the prompt-change pin

**Terminal:** T5 · **Phase:** P3b · **Wave:** 2 · **Date:** 2026-10-05

---

## 0. The verdict, stated first

| | |
|---|---|
| **G3b** | **`pass` with a `blocked` sub-item** — the ablation number exists and is publishable; the cache-cost interaction is `blocked` on a live provider |
| **Tools ablation** | **`MEASURED`. Net: prompt tokens down, capability intact for a model that searches, capability gone for one that does not — and the price is paid in round trips and cache misses.** |
| **Headline number** | **`-10.8%` prompt tokens on the default query, `-11.59%` mean across six queries, range `0.00%` to `-27.23%`. NOT 40% — see §7.** |
| **The 40% claim** | **It exists, and it is T1's number from a DIFFERENT mechanism. See §7 — two deferral authorities, 2520 tokens apart.** |
| **Prompt pin** | **Demonstrated firing and not firing. One bypass FOUND AND CLOSED that was the whole gate.** |
| **Model caveat** | **Every model call is a scripted double. No number in this document is a claim about model quality.** |

---

## 1. Non-vacuity, verified before anything was run

The brief's trap is real and it is two traps, not one.

### 1.1 The shipped mock cannot exercise this at all

`verify_vacuity()` re-derives this from source on every run:

```
"runtime/mock_provider.py": {"readable": true, "mentions_tools": false, "occurrences": 0}
"evals/run.py":             {"readable": true, "mentions_tools": false, "occurrences": 0}
"synthesize": "(model: 'str', messages: 'list') -> 'Optional[str]'"
"all_function_signatures": ["install(responses, dynamic)", "reset()", "is_active()",
                            "synthesize(model, messages)", "make_scripted_model_fn(script_spec)",
                            "install_script(script_spec)", "fn(messages, model)"]
"success_axis_vacuous_under_shipped_mock": true
"agrees_with_record": true
```

The scripted responder's signature is `fn(messages, model)` — **no `tools` parameter**. The
schema list is dropped at the model boundary, so a task outcome *cannot* move between arms.
`agrees_with_record: true` means a future change to the mock that made this axis non-vacuous
would be **detected**, not silently obsoleting the claim.

**Model used: `evals.tools_ablation.SchemaAwareModel`** — scripted-deterministic, signature
`fn(messages, model, tools=None, **kwargs)` which **names** `tools` and reads it. No socket, no
credential, bit-identical run to run — and unlike the shipped mock it can be *hurt* by deferral.

### 1.2 The go/no-go metric could not fail. Three separate reasons.

**This is the most important thing this wave found.** The brief names
`discoverability_failure_rate` as the go/no-go, and on arrival it was **structurally 0.0** — a
metric that cannot fail is not a go/no-go.

| # | defect | how it was found | fix |
|---|---|---|---|
| **a** | `measure_arm` called `resolve_selection` **unconditionally** in both arms and scored a needed tool reachable whenever it appeared in the result. `select:<name>` resolves by **exact lookup**, so *any* catalog tool resolved. | swept `discoverable` over `[needed]`, `[]`, `["something_else"]`, `None` → **0.0 in all four** | a search route counts only if the model **actually searched** (`model.searches` non-empty) **and** that search's own resolution returned the tool |
| **b** | the injected fixture description was `f"the {name} tool"`. Under the shipped substring scorer `term in description` has **no word boundary and no stopword list**, so "the" matched almost any English query → the tool was KEPT for a reason unrelated to the task. | derived the decision rather than reading it | `MEASURED_TOOL_DESCRIPTION`, a real sentence — **and that was still not enough, see (c)** |
| **c** | even a realistic English description scores **6.0** against *"explain what this module does and summarise the architecture"*, because every English sentence contains "the"/"a"/"an". **An English-described tool is essentially never deferred under the shipped default.** | wrote the realistic description, re-measured, still 0.0 | `DERIVABLE_DESCRIPTION` — a string with **no `a`, `e`, `i`, `o`, `u`, `y` at all**, derived not asserted |

Defect (c) is worth stating on its own: **the substring scorer has no word boundary, so the
one-character query term "a" in "write a release note" matches any description containing the
letter "a".** The first attempt at a "stopword-free" fixture (`quorum ledger shard state
reconciliation`) contained "state", so it matched — and the derivation silently fell back to
the degenerate empty query, which would have looked like a passing test and measured a blind
agent. That is pinned by `test_the_derivable_description_is_genuinely_unmatchable`.

### 1.3 The arms differ in exactly one variable — proven by diff

```
single_variable=SINGLE_VARIABLE differing=['tool_deferral_enabled']
```

And the check runs on the **effective** configs the run actually used, not the module-level
`ARMS` — a check run on a dict the measurement did not use is a claim about a diff rather than
the diff. `IDENTICAL_ARMS` (two equal arms) is reported as the vacuity trap with
`attributable: False`, pinned by `test_two_identical_arms_are_reported_as_the_vacuity_trap`.

---

## 2. The tools ablation — all seven metrics

Real 48-tool catalog (`harness.tools.typed_tool_specs()` + one measured tool + one withheld +
one discovery tool). Token figures are a `CHARS_PER_TOKEN` heuristic over the **serialised**
schema list, never `len(tools)`.

### 2.1 The default query

`read the configuration file and report the timeout` · scorer `substring` (module default)

| metric | eager | deferred | delta |
|---|---|---|---|
| **prompt tokens** | 3,659 | 3,264 | **−395 (−10.8%)** |
| **tool-schema tokens** | 3,647 | 3,252 | **−395 (−10.8%)** |
| **resolve rate** | 1.0 | 1.0 | 0.0 |
| **model calls** | 1 | 1 | 0 |
| **cost** | `unpriced` | `unpriced` | **not measurable** |
| **discoverability failure rate** | 0.0 | 0.0 | 0.0 |
| **skill-load rate** | — | — | **NOT MEASURED BY THIS DRIVER** |
| extra round trips | 0 | 0 | 0 |

schemas offered 48 → 42 · deferred 5 · withheld 1 · `cost_priced: False`

`prompt_tokens` = schema tokens + `len(query) // 4`; `tool_schema_tokens` is the serialised
schema list alone. They differ by only the query's own 12 tokens, which is why the deltas are
identical.

### 2.2 Six queries × 5 repetitions — the table the brief asked for

| query | schemas e/d | schema tokens e/d | delta | % | deferred | resolve e/d | discoverability e/d | calls | cost |
|---|---|---|---|---|---|---|---|---|---|
| matches_the_needed_tool | 48/42 | 3,647/3,252 | −395 | **−10.83%** | 5 | 1.0/1.0 | 0.0/0.0 | 1/1 | unpriced |
| does_not_match_it | 48/38 | 3,647/2,845 | −802 | **−21.99%** | 9 | 1.0/1.0 | 0.0/0.0 | 1/1 | unpriced |
| unrelated_short | 48/46 | 3,647/3,589 | −58 | **−1.59%** | 1 | 1.0/1.0 | 0.0/0.0 | 1/1 | unpriced |
| unrelated_docs | 48/35 | 3,647/2,654 | −993 | **−27.23%** | 12 | 1.0/1.0 | 0.0/0.0 | 1/1 | unpriced |
| unrelated_tests | 48/44 | 3,647/3,358 | −289 | **−7.92%** | 3 | 1.0/1.0 | 0.0/0.0 | 1/1 | unpriced |
| empty | 48/48 | 3,647/3,647 | 0 | **0.00%** | 0 | 1.0/1.0 | 0.0/0.0 | 1/1 | unpriced |

**Spread across repetitions is `0.0` on every axis, `n_distinct: 1`.** That is stated as what it
is: `spread is 0.0 across repetitions because the driver is scripted-deterministic; that is a
property of the measurement, not evidence about a real model.`

**The variation that matters is across queries**, and it is large:

| arm | mean schema tokens | spread | n_distinct |
|---|---|---|---|
| eager | 3,647.0 | 0.0 | 1 |
| **deferred** | **3,224.2** | **993.0** | **6** |

Mean saving **−422.8 tokens (−11.59%)**, and the saving ranges from **0.00% to −27.23%**
depending on the query. **A single run on a single query would have published any number in that
range as "the" number.** That is why the query sweep is the experiment and N repetitions of one
query is not.

### 2.3 The discoverability go/no-go, driven to both ends

```
go/no-go axis: non_vacuous = True
  capability intact when the model searches   = True
  capability lost when it does not search     = True
```

Measured over the tools deferral **actually** removes (9–47 per query/scorer), not a hand-picked
one:

| model behaviour | arm | discoverability failure rate | round trips |
|---|---|---|---|
| searches when it needs a tool | eager | 0.0 | 0 |
| searches when it needs a tool | **deferred** | **0.0** | **N** (N = tools deferred) |
| never searches | eager | 0.0 | 0 |
| never searches | **deferred** | **1.0** | 0 |

**That is the whole trade, and it is the finding.** Deferral is capability-**neutral** for a
model that searches and capability-**destroying** for one that does not. The lever this phase
pulled is not "defer the tools" — it is **"make the search seam reliable"**, and **no number in
this document measures the second thing.**

The round-trip column is the cost the token headline hides. `extra_round_trips_for_discovery`
equals the number of deferred tools the model recovered, and on a real provider those are
latency and money exactly like the tokens are. Reported together deliberately, so a token win
cannot be read as a latency win.

### 2.4 Skill-load rate — NOT MEASURED, and said so

The tools ablation does not measure skill loading; it is a different subsystem
(`harness.skills` / `extensions.skill_registry`). The honest-absent rule for it is defined and
asserted (`shared/p3b_truth.py`, metric `skill_load_rate`), and **no number is quoted here.**
T1's budget table carries a skill-index row; that is a prompt-composition measurement, not a
skill-load rate.

### 2.5 The scorer audit — the number is scorer-dependent

```
query                      scorer      kept  deferred  stopword-only
matches_the_needed_tool    bm25          39         8            41%
matches_the_needed_tool    substring     42         5            43%
                           jaccard    0.9285714285714286
does_not_match_it          bm25          36        11            83%
does_not_match_it          substring     38         9            84%
                           jaccard    0.9473684210526315
unrelated_short            bm25          31        15            94%
unrelated_short            substring     46         1            96%
                           jaccard    0.6739130434782609
unrelated_docs             bm25          32        15            84%
unrelated_docs             substring     35        12            86%
                           jaccard    0.9142857142857143
unrelated_tests            bm25          37        10            51%
unrelated_tests            substring     44         3            45%
                           jaccard    0.8409090909090909
empty                      bm25           0        46              -
empty                      substring     47         0           100%
                           jaccard    0.0
```

Mean Jaccard **0.861** — the two registered scorers agree on most of the catalog, but **not all
of it**, and the `empty` row agrees on *nothing*.

**`stopword-only` is the number that matters.** Under the shipped default, **41%–100% of the
kept set is kept by an English function word rather than by relevance.** The deferral decision
is substantially a measurement of the *scorer*, not of the query.

**The default scorer was NOT switched to BM25.** That is a measured product decision with its own
arms, and switching it here would move this phase's headline in the flattering direction without
anybody having decided to move it.

### 2.6 The net verdict

> **Prompt tokens: down 10.8% on the default query, 11.59% mean across six queries, range 0.00%
> to 27.23%. Capability: intact (0.0 failure rate) for a model that searches; entirely gone (1.0)
> for one that does not. Cost: one extra model round trip per deferred tool recovered, plus a
> cache miss on the following turn for the same reason.**
>
> **Verdict: `MEASURED`. A conditional win, and the condition is the whole result.** It is a win
> only if the model reliably searches — which this driver cannot observe, because the model is a
> scripted double whose search behaviour I supplied. **The interesting number is not −10.8%; it
> is the 0.0 / 1.0 split.**

**What this is not.** It is not evidence that AHEAD's +3.3pp for tools is real or false — that
is another stack's measurement of another system. It is not a cost measurement: `cost_usd` is
the string `unpriced` in every arm, `cost_priced: False`, because no provider ran and a `$0.00`
reads as *cheapest* to a price ladder. It is not a model-quality measurement of any kind.

---

## 3. The prompt budget table at turns 1 / 10 / 40 / 80

From `harness.prompt_budget_measure` (T1), catalog fingerprint `d3eb4ac7de866736`, estimator
`heuristic`, window 32,768. Threshold **FIRED**: schemas are 12.91% of the window, over the 10%
configured share.

| | turn 1 | turn 10 | turn 40 | turn 80 |
|---|---:|---:|---:|---:|
| Tool schema tokens (before) | 3,592 | 3,592 | 3,592 | 3,592 |
| Skill body tokens (before) | 0 | 0 | 0 | 0 |
| **Total prompt tokens (before)** | **3,981** | **8,491** | **8,878** | **6,505** |
| Tool schema tokens (after) | 735 | 735 | 735 | 735 |
| Skill index tokens (after) | 4 | 4 | 4 | 4 |
| **Total prompt tokens (after)** | **1,128** | **5,638** | **6,025** | **3,652** |
| Δ total | −2,853 | −2,853 | −2,853 | −2,853 |
| **Δ %** | **71.7** | **33.6** | **32.1** | **43.9** |

**Both numbers are measured and both carry provenance**, which is the requirement
`shared.p3b_truth.reduction_row` enforces:

| | value |
|---|---|
| before_total (4 depths) | 27,855 |
| after_total | 16,443 |
| delta_total | −11,412 |
| **gross reduction** | **40.97%** |
| tool-search discovery overhead | 992 tokens |
| **net_total** | **17,435** |
| **net reduction** | **37.41%** |
| calls where a deferred tool was needed | 8 |
| extra round trips | 0 |

**So "we cut the prompt 40%" is defensible — as `40.97%` gross / `37.41%` net, against a
measured 27,855-token baseline, with the discovery cost SUBTRACTED rather than footnoted.**

**Three caveats that must travel with it:**

1. The non-schema conversation component is **BORROWED** from P3a's measured growth, not
   re-simulated, unless a caller supplies `conversation_growth`.
2. Skill rows are measured over a **fixed synthetic set of 5 skills** so the table is
   reproducible across machines. It is a prompt-composition measurement.
3. Discovery overhead is charged **once** against the sum of four depths while the savings are
   counted at each depth. That understates the mechanism deliberately; an amortised cost would
   flatter it.

---

## 4. The cache interaction — `blocked`, with the mechanism measured

T3's own authority (`runtime.cache_economics`, `runtime.prompt_cache`). No second arithmetic path.

### 4.1 What is `blocked`, and why

**No live provider was reachable** (T3 recorded `ServiceUnavailableError: No available channel`
on 3/3 live completions). Therefore:

| axis | value | **not** |
|---|---|---|
| `cache_hit_rate` | **unreported** | ~~0%~~ |
| `cost_usd` | **unpriced** (`priced: False`) | ~~$0.00~~ |
| net cache benefit | **"the cache benefit is UNKNOWN, not zero"** | ~~0~~ |

T3's four honest-absent rules all hold: `unreported_is_not_a_zero_hit_rate`,
`no_session_sum_without_the_call_count`, `unpriced_is_never_zero_dollars`,
`spent_is_a_maximum_never_a_sum`.

### 4.2 What IS measured, and it is not nothing

The three real compositions, through `plan_cache()`:

| arm | tools | prefix tokens | tool_schema_sha256 |
|---|---:|---:|---|
| eager catalog | 45 | 3,560 | `9867784bf6c3d2e0` |
| **T1's shown set** (`should_defer`, threshold fired) | **8** | **640** | `a4b0aeb111a66c2f` |
| **integrations' kept set** (`DeferralPolicy` @ 0.0) | **43** | **3,306** | `ee0473f35898bc0e` |

**And the interaction itself, which is the finding:**

```
turn 1 (deferred set offered)  tool_schema_sha=ee0473f35898bc0e
turn 2 (after ONE discovery)   tool_schema_sha=d5cbd601e74040e0
prefix_tokens 3306 -> 3362
KEY CHANGED: True
```

**Every deferred tool the model recovers is a cache MISS on the next turn.** The token saving
and the cache hit rate are *the same event counted from two directions*, and on a real provider
they trade directly against each other. The smaller prefix is cheaper per hit and invalidated
more often. **The net is not computable from this driver** — no observed `cache_read`
input_tokens, and a scripted model performs no discovery *during* a turn, so the invalidation
events a real run generates do not occur here.

This is a **real, previously unstated cost of deferred loading**, and it is the strongest
argument in this document for Phase 4's middleware work.

---

## 5. The prompt-change pin — demonstrated, and one bypass found

`python -m evals.prompt_pin` — **AST-digests the string VALUE of 9 agent-facing constants** in
`harness/prompts.py`, so a reformat or a docstring edit is not a prompt change and a
one-character prompt edit is.

### 5.1 Transcript A — it FIRES on an unarmed edit

```
### 1. UNARMED EDIT - one sentence appended to DAILY_SYSTEM, no arm added
prompt pin: PROMPTS_UNARMED
  constant                            chars state            arms
  DAILY_SYSTEM                          457 unarmed_prompt_edit baseline
  [unarmed_prompt_edit] DAILY_SYSTEM: agent-facing prompt 'DAILY_SYSTEM' changed and the
  ONLY arms exercising it are ['baseline'], which are control arms: every arm in the matrix
  would see the same text, so no comparison can detect this change. Add an arm that differs
  from baseline and touches this prompt, list it in ARM_COVERAGE['DAILY_SYSTEM'], then move
  its digest with `python -m evals.prompt_pin --update`
  EXIT CODE: 2
```

### 5.2 Transcript B — it does NOT fire on a legitimate edit

```
### 2. REVERTED - tree restored, no edit
prompt pin: PROMPTS_ARMED
  DAILY_SYSTEM                          430 unchanged        baseline
  EXIT CODE: 0

### 3. REFORMAT ONLY - whitespace/indentation churn, no prompt text change
prompt pin: PROMPTS_ARMED
  DAILY_SYSTEM                          430 unchanged        baseline
  EXIT CODE: 0

SUMMARY: unarmed=2 reverted=0 reformatted=0
```

And the inverse is asserted too, because it is the half people get wrong:
`test_whitespace_INSIDE_the_prompt_does_fire` — a blank line added **inside** the triple-quoted
literal changes what the model reads, so the gate **must** fire. A file-level digest would fail
that, which is why the gate reads values.

**This tree is armed:**

```
prompt pin: PROMPTS_ARMED
registered arms: 10
exemptions: 2/2 (AGENT_TESTS_SYSTEM, SELF_CRITIQUE_SYSTEM)
coverage gaps: 7 (CONSTRAINT_REINJECTION_TEMPLATE, CONTEXT_REINJECTION_TEMPLATE,
                  DAILY_SYSTEM, DAILY_USER_TEMPLATE, PLANNER_COORDINATION_RULES,
                  PLANNER_USER, STEERING_MSG_TEMPLATE)
findings: 0
```

### 5.3 The bypass that worked, and is now closed

**Found by attacking the gate, not by reading it.**

> Delete `DAILY_SYSTEM` from `ARM_COVERAGE`, run `--update`, paste the digest.

Every pre-existing rule was satisfied: the digest matched, so there was no edit to report;
`MIN_PINNED_CONSTANTS` was satisfied because a digest was **moved** rather than deleted;
`CONTROL_ONLY_ARMS` was never consulted because there was no coverage entry left to consult;
`EXEMPTIONS` was untouched. **The gate returned `ok: True` on an unarmed edit to the loop's
system prompt** — the exact −2.3pp change it exists to police — because its own coverage table
was edited in the same commit.

Transcript of the closed bypass:

```
BASELINE: clean tree
  prompt pin: PROMPTS_ARMED
  DAILY_SYSTEM                          430 unchanged        baseline
  EXIT CODE: 0

BYPASS ATTEMPT: coverage entry DELETED + digest moved by --update
  prompt pin: PROMPTS_UNARMED
  DAILY_SYSTEM                          457 unclassified     -
  [unclassified_prompt] DAILY_SYSTEM: agent-facing prompt 'DAILY_SYSTEM' is reviewed but is
  in neither ARM_COVERAGE nor EXEMPTIONS. Removing a coverage entry is how this gate gets
  disarmed: with no entry there is no coverage set to check against CONTROL_ONLY_ARMS, so
  moving the digest alone makes an unarmed edit pass. Classify it, or record it as a named
  exemption with a reason and an owner
  EXIT CODE: 2

BYPASS COMPLETE: coverage deleted AND digest moved (the full attack)
  prompt pin: PROMPTS_UNARMED
  EXIT CODE: 2

RESTORED
  prompt pin: PROMPTS_ARMED
  EXIT CODE: 0

SUMMARY: baseline=0 coverage_deleted=2 full_attack=2 restored=0
```

**The fix is `REVIEWED_AGENT_FACING`** — the set of constants the gate must have an opinion
about, written out rather than derived, because *deriving it from `ARM_COVERAGE` is precisely
the bypass*. The two rules are deliberately redundant; redundant is the point. And the
`unclassified` **state** is now checked **before** the digest, so the per-constant table shows
why.

### 5.4 Every other bypass, and what refuses it

| attack | refused by | verified |
|---|---|---|
| add an exemption with reason+owner | the digest still moves → `stale_exemption` is a **finding**, not a pass | exit 2 |
| empty `PINNED_DIGESTS` | `MIN_PINNED_CONSTANTS` | exit 2 |
| wildcard/glob exemption | `_wildcard_exemption_findings` | finding |
| exemption with no reason / no owner | same, one finding **per field** | finding |
| move the prompt to another module | `prompt_unreadable` | exit 2 |
| construct the prompt at runtime | `prompt_unreadable` | exit 2 |
| dangling arm in a coverage entry | `dangling_arm_coverage`, fires with no digest change | finding |
| **shrink the coverage table** | **`REVIEWED_AGENT_FACING`** | **exit 2 — was open** |

### 5.5 Exemptions are explicit, reviewed, counted, and pinned

```
exemptions: 2/2 (AGENT_TESTS_SYSTEM, SELF_CRITIQUE_SYSTEM)
```

`MAX_EXEMPTIONS = 2` is a **pinned constant**, and exceeding it is a finding:

```
STEP: ONE extra exemption added (expect exit 2 - the bound fires)
  prompt pin: PROMPTS_UNARMED
  exemptions: 3/2 (AGENT_TESTS_SYSTEM, PROJECT_INSTRUCTIONS_SYSTEM, SELF_CRITIQUE_SYSTEM)
  [invalid_exemption] -: the exemption list holds 3 entries, over the pinned maximum of 2.
  Every entry needs a reason and an owner, and that is what makes the list reviewable;
  without a bound, reviewability decays silently because each individual entry still looks
  justified. Raise MAX_EXEMPTIONS deliberately, in this file, so the change appears in a diff
  on the rule
  EXIT CODE: 2
```

`test_the_exemption_count_matches_the_pinned_maximum_today` makes **growth** a visible diff on
the rule rather than a longer list nobody reviewed. Both entries say `no eval arm` in their
reason, so they read as gaps rather than as decisions.

**The 7 coverage gaps are a real hole in the eval matrix** and the gate is the only thing keeping
them named. They are published on every run, asserted non-empty, and the gate refuses any edit to
them.

### 5.6 In the smoke lane

Four steps added to the `smoke` job (blocking, `<30 s`, one AST parse, no Docker, no network, no
test collection), all verified to run here:

```yaml
- name: "GATE: no unarmed agent-facing prompt edit"
  run: python -m evals.prompt_pin
- name: "GATE: prompt-pin exemption count and coverage gaps are published"
- name: "GATE: the discoverability go/no-go axis can actually fail"
  run: python -m evals.tools_ablation --go-no-go
- name: "GATE: the registered scorers agree on something"
  run: python -m evals.tools_ablation --scorer-audit
```

`python -m evals.prompt_pin` → 0 · `--go-no-go` → 0 · `--scorer-audit` → 0 ·
`tests.known_failing_pins` → 0 · `evals.ci_lanes` → `CI_LANES_CONSISTENT`.

---

## 6. The six honest-absent rules, and reduction-requires-a-baseline

`shared/p3b_truth.py` — one authority, `python -m shared.p3b_truth`, exit 2 when a renderer
disagrees. **Every rule is asserted in BOTH directions**, because a rule that only checks the
absent path is satisfied by a function that returns a constant.

| metric | absent value | renders as | the lie it prevents |
|---|---|---|---|
| `prompt_tokens` | `None` | `unavailable` | `0` when the estimator never ran |
| **`prompt_reduction_pct`** | `None` | `unavailable` | **a percentage computed against a missing or unprovenanced baseline** |
| `cache_hit_rate` | `None` | `unavailable` | `unreported` collapsing to `0%` |
| `discoverability_rate` | `None` | `unavailable` | `0` (never measured) reading as `0%` (measured, genuinely zero) |
| `skill_load_rate` | `None` | `unavailable` | `0%` for a scan that never ran |
| `deferred_count` | `None` | `unavailable` | `0` when deferral did not run at all |

**The priority rule is mechanical.** `reduction_row(before, after, provenance)` refuses to
publish a percentage unless **both** values exist **and** the baseline carries
`measured_at` + `source`. An absent value **omits its `value` key entirely**, so a consumer doing
`data["value"] or 0` gets a `KeyError` — the right outcome for a number nobody took. A `bool` is
passed through rather than coerced: the first version coerced, so `reduction_row(True, 100)`
computed a confident **−9900%** off a flag the caller meant as "did we measure this".

```python
>>> r = reduction_row(None, 1128)                    # an 'after' with no 'before'
>>> r.computable
False
>>> r.to_dict()["reduction_pct_is"]
'unavailable'
>>> "reduction_pct" in r.to_dict()                   # a baseline is a rumour with a digit in it
False
```

`summarise()` refuses to sum over a thinning denominator silently: it carries `n_available`,
`n_absent` and `absent_metrics`, and a total over zero available rows is `None`, not `0`.

**T4's rendering verified by CALLING it, not by editing `cli/`:**

```
cli.context_budget.UNAVAILABLE                          literal='unavailable' literal_matches=True
cli.context_budget.budget_rows                          is_gate=True
cli.cache_cost.assert_absent_is_never_zero              is_gate=True
cli.capability_view.assert_no_false_capability          is_gate=True
cli.skill_view.assert_no_skill_claims_a_body_it_did_not_load  is_gate=True
verdict: HONEST_ABSENT_HOLDS
```

`cli/context_budget.py`'s `UNAVAILABLE` literal is asserted **identical** to the shared one —
two literals that drift is how a UI stops matching its own gate.

---

## 7. The finding that outranks the headline: TWO DEFERRAL AUTHORITIES

**There are two deferral mechanisms in this tree, they disagree, and the phase brief's headline
number comes from the one that is not wired to config.**

| | `harness/deferred_tools.should_defer` | `integrations/tool_deferral.DeferralPolicy` |
|---|---|---|
| decision | binary: on the `EAGER_TOOLS` floor or not | scored, per-tool, against a threshold |
| trigger | `collapsing=threshold.fired` | `tool_deferral_enabled` config key |
| tools shown | **8** | **42** |
| schema tokens sent | **735** | **3,252** |
| delta on the full catalog | **−3,495 (−82.6%)** | **−395 (−10.8%)** |

**The gap is 2,520 tokens on the same query with the same catalog.** The only tools in the
first set and not the second are 34 names.

**Which one does a config key select?** `integrations.tool_deferral.selection_from_config({"tool_deferral_enabled": True}).enabled` → `True`. `harness.prompt_budget_measure` reads `DEFAULT_SCHEMA_SHARE_THRESHOLD` and calls
`should_defer(collapsing=threshold.fired)` — **a different function.**

So: **the 40% reduction is measured through the eager-floor mechanism; the −10.8% is measured
through the config-wired mechanism.** Both are real measurements of two different systems. Phase
3b's headline currently describes the one that is not reachable by configuration.

**This is `DOCTRINE.md` §6 exactly** — *"One authority per concern. When two modules both compute
something, one of them is wrong. Find the duplicate before adding a third."* — and I have found
the duplicate. **I did not merge them**: `harness/` is untouchable this wave, and picking a
winner is a product decision with its own arms. It is filed in §9.

**And the scorer defect that made the second authority look weak:** BM25 — a *registered,
selectable* scorer — returned **0 of 45 hits on every query**, because
`integrations/tool_search.with_tokens` guarded on `name_tokens and tag_tokens`, and a tool with
**no tags** has `tag_tokens == ()`, which is falsy. So the guard never passed, every call minted
a new object, `fit()`'s identity lookup missed, and `score()` returned `NO_MATCH` for everything.
The consequence was not a degraded ranking but an **inverted policy**: with every tool
`NO_MATCH`, `DeferralPolicy` classifies all 45 as `no_query_match` and **defers the entire
catalog**. `deferral_safety_report` does not catch it — the discovery tool is still reachable, and
the report checks that a discovery path exists, not that anything was left in the request.

**Fixed** (guard moved to `description_tokens`, which is non-empty for any candidate with a
description). All 45 real harness tools are built with `tags=()`, so the dead path was the only
path this catalog had. After the fix, Jaccard against substring is 0.67–0.95. Pinned by
`test_with_tokens_is_idempotent_for_every_real_tool`.

---

## 8. The capability-loss risk — the discoverability numbers

| row | value |
|---|---|
| Discoverability failure rate, **model that searches** | **0.0** — capability intact |
| Discoverability failure rate, **model that never searches** | **1.0** — capability entirely gone |
| Extra round trips per recovered deferred tool | **1** (8 tools → 8 round trips on the 8-need case) |
| Tools deferral removes, range across queries | **0 – 12 of 48** |
| Saving range across queries | **0.00% – 27.23%** |
| Stopword-only share of the kept set | **41% – 100%** |
| Scorer agreement (mean Jaccard) | **0.861** |
| Deferral authority disagreement | **2,520 tokens** |

**The go/no-go: capability survives, but only conditionally, and the condition is not measured
by anything in this tree.** Whether a real model searches for a tool it was not shown is a
model-behaviour question, and every model here is a scripted double. That is the honest limit
of this wave's headline.

---

## 9. LOC delta

| file | change |
|---|---|
| `shared/p3b_truth.py` | **+572** (new — the six rules) |
| `tests/test_p3b_w2_truth.py` | **+1,180** (new — 104 tests) |
| `evals/tools_ablation.py` | +~1,000 (untracked; Wave 1's 926 + this wave's additions) |
| `evals/prompt_pin.py` | +~150 (untracked; `REVIEWED_AGENT_FACING`, `MAX_EXEMPTIONS`, counters) |
| `integrations/tool_search.py` | +28 / −4 (the `with_tokens` guard) |
| `tests/test_p3b_tools_and_prompts.py` | +~100 (adjudication fixes) |
| `.github/workflows/ci-lanes.yml` | +166 / −2 (mine is +25; file is shared and dirty) |
| `evals/ci_lanes.py` | +124 / −3 (mine is +20; file is shared and dirty) |

**Net: +2,900 / −9 LOC.** `git diff --numstat` over-reports on `ci-lanes.yml` and `ci_lanes.py`
because three other terminals have them dirty in the same working tree; my additions are called
out separately above.

---

## 10. What is blocked, and the scripted-model caveat

> **EVERY MODEL CALL IN EVERY NUMBER IN THIS DOCUMENT IS A SCRIPTED DOUBLE.**
>
> `SchemaAwareModel` is deterministic by construction — no clock, no randomness, no I/O — and
> every field it reports is derived from its arguments, so two runs are bit-identical. That is
> **why** `spread = 0.0` across repetitions, and it is a statement about this codebase, not about
> any model.
>
> **No credential was read, printed or retained. No live provider ran. Therefore no number here
> is a claim about model quality, model cost, or model latency.**

| blocked | reason | owner |
|---|---|---|
| `cache_hit_rate` before/after | **blocked** — no live provider; `ServiceUnavailableError: No available channel` on 3/3. Reported `unreported`, **not** `0%`. | needs a credential |
| net cost per turn | **blocked** — no priced model. `cost_usd: unpriced`, `priced: False`, **not** `$0.00`. | needs a credential |
| whether a real model searches for a deferred tool | **not measurable** — the search behaviour is supplied by the driver | Phase 4 |
| `skill_load_rate` for the tools ablation | **not measured** — a different subsystem | T2 |
| which deferral authority should win | **open** — a product decision, not a measurement | §7 |

**Also blocked, and it is not this wave's code.** `python -m evals.run --suite trust-ladder`
exits 2 with `MEASURED_RED`, **5 pass / 5 fail / 0 blocked**:

| rung | status | why |
|---|---|---|
| 3 | fail (carried) | the pin registry exited 1: `harness/test_mint_site_pins.py` fails to collect *under the ladder's subprocess*, on a docstring line 10 (`Google diff-match-patch's`) that is **not** a syntax error. Running that file directly: **14 passed**. A cwd/encoding difference in the rung's own subprocess, not a broken pin. |
| 4 | fail (carried) | `tests/test_aesthetic_gate.py` — 6 nodes red, another terminal's in-flight work. |
| 6 | fail (carried) | `VEX-ADV-0001` active: litellm pinned 1.74.9 < fixed 1.74.10. |
| 7 | fail (measured) | **P3a's own finding, unchanged**: 9 of 14 tasks recalled nothing, staleness 0.166667, 3 of 3 moved outcomes for the worse. |
| 9 | fail (measured) | cold retrieval 10,243 ms against a 1,000 ms budget on this machine, **phase attribution INCONCLUSIVE and deliberately not reported as "no cause"**. |

None of these were fixed by editing another terminal's file to make a red go green.

---

## 11. Phase 4 brief — long-horizon autonomy

**Read this next, because it changes what Phase 4 should optimise.**

### 11.1 What Phase 4 inherits, measured

| inherited finding | number | what it means for Phase 4 |
|---|---|---|
| Deferral capability is conditional on searching | **0.0 vs 1.0** | the lever is the **search seam**, not the token count |
| Every recovered deferred tool is a cache miss | **key changes on discovery** | the middleware pipeline has to decide whether a discovery re-prices the turn |
| Round trips per recovered tool | **1 each** | middleware is where that cost is amortised or not |
| Two deferral authorities | **2,520 tokens apart** | **fix this before optimising either** |
| Scorer is stopword-blind | **41–100%** of keeps | relevance ranking is not doing what its name says |
| Prompt prose | **−2.3pp** (AHEAD, this repo's prompt) | every prompt addition is now gated, and the gate is proven to fire |

### 11.2 The number Phase 4 has to beat

**AHEAD's middleware lever: +2.2pp.** That is the bar.

### 11.3 The four Phase-4 deliverables, each with the measurement it must produce first

1. **The initializer agent.** Measure: does it reduce the first-turn cold cost
   (rung 9's 10,243 ms) or does it add a turn? **Assert the direction and a generous bound,
   never a tight absolute threshold** — rung 9's own budget failure is the cautionary tale.
2. **The middleware pipeline.** Measure: **does a discovery's cache miss get amortised?** The
   `plan_cache` interaction in §4.2 is the concrete question. A middleware that re-prices the
   prefix on every discovery trades the token saving straight back.
3. **Subagent isolation.** Measure: **what does a subagent do to the parent's cache key?** If a
   subagent's tool surface differs, `tool_schema_sha256` differs, and the parent's prefix is
   invalidated. This is not measured and should be.
4. **Hybrid stuck detection.** Measure: on the 168-run P3a corpus, how many of the 3
   worse-outcomes would it have caught, and **how many false positives on the 165 that were
   fine**. A detector that flags everything has a recall of 1.0 and is worth nothing.

### 11.4 The three rules carried forward

1. **Every model call is a double until a live lane exists.** Phase 4's first deliverable should
   be a lane that can produce ONE real number, not a mechanism.
2. **A metric that cannot fail is not a gate.** §1.2 found three such metrics in this phase's
   own instrument. Budget for proving each Phase-4 metric can move before trusting it.
3. **One authority per concern.** Two deferral authorities shipped in the same phase. Phase 4
   adds a middleware pipeline and a stuck detector; if either of them grows a second
   implementation of something `harness/` already does, the same 2,520-token disagreement
   recurs.

---

## 12. The raw verdict

```
G3b: pass with a blocked sub-item
  pass                the tools ablation number exists, is non-vacuous, and is publishable
  pass                the prompt pin fires and does not fire; the coverage-shrink bypass is closed
  pass                the six honest-absent rules are defined, asserted both ways, and T4 honours them
  pass                reduction-requires-a-baseline is enforced mechanically
  blocked             cache hit rate and net cost per turn — no live provider (not 0%, not $0.00)
  not_implemented     whether a real model searches for a deferred tool — needs Phase 4
  not_implemented     which of the two deferral authorities should win — needs a product decision
```

**Never `skip`. Every row above is `pass`, `blocked`, or `not_implemented` with an owner.**
