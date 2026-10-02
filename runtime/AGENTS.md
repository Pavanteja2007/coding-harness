## P1/W1 T3 — the 18-second import, the latency ledger, and the turn-cap verdict (2026-10-02)

**NEW `runtime/latency.py` (827 lines), NEW `runtime/startup_audit.py` (445),
NEW `runtime/test_latency_pins.py` (1030, 60 tests). Edited:
`runtime/model_router.py`, `runtime/model_capabilities.py`,
`runtime/__init__.py`.** Nothing outside `runtime/**` was touched. **No live
provider lane and no Docker lane were run, and neither is claimed.**

### 0. The brief's premise is wrong in three places, and the corrections matter more than the fix

The brief said the import "sits on the live model-call path at
`runtime/model_router.py` around lines 1000 and 1037" and costs **13.09 s**.
Measured, all three are wrong:

| the brief said | measured | what it changes |
|---|---|---|
| the import is on the import path | it was **already lazy everywhere** — every `import litellm` in `runtime/` is inside a function. `import runtime.model_router` = **0.19 s median** and `litellm` is **absent** from `sys.modules` afterwards | the fix is not "make it lazy", it is "stop paying it twice" |
| 13.09 s | **6.1 – 22.5 s** across 20+ cold runs; **9.76 s median** (min 8.94, max 10.58, n=5); the tree's own history records 13 s, 15.36 s, 17–26 s and 32–34 s | quote a range, never a number |
| strategy B ("lazy submodule access") is a candidate | **refuted by measurement**: `import litellm.llms.openai.chat`, `.main`, `.utils`, `.caching.caching` all load the **identical 2239 modules** at 13.5–16.9 s. CPython executes the parent package's `__init__` first, so there is no cheaper submodule | strategy A is the only option short of vendoring |

**The variance is a NETWORK call, and that is the most useful finding here.**
litellm's `__init__` calls `get_model_cost_map()`, which `httpx.get()`s a
4 440-entry price table from `raw.githubusercontent.com` with a **5-second
timeout** (a source fact, read from
`litellm/litellm_core_utils/get_model_cost_map.py`) and falls back to the
bundled copy. Measured self-time inside `litellm/__init__.py`:

| | median | min | max | n |
|---|---|---|---|---|
| default (network fetch) | 0.29 s | 0.28 s | 0.34 s | 5 |
| `local_cost_map=True` | 0.31 s | 0.28 s | 0.46 s | 5 |

**This corrects an earlier reading of this round.** A first measurement showed
5.98 s of self time and concluded the lever was worth 5.9 s. It is not — that
was ONE cold-TLS sample, and the warm median is 0.29 s. The 13 s→35 s spread
across this repo's history is network variance on that one fetch, not module
count. `LITELLM_LOCAL_MODEL_COST_MAP` is therefore offered for the
**reproducibility** reason (startup should not depend on a fetch from
github.com; two runs on different days can otherwise see different price
tables) and **not as a latency claim**. It is off by default.

### 1. What landed

| file | what |
|---|---|
| `runtime/latency.py` | the ONE authority: `ensure_litellm` (the only litellm import in `runtime/`), `start_preload`, `preload_state`, `preload_eligible`, `module_present` / `module_initialised`, and the `CallLatency` / `LatencyWindow` / `percentiles` receipt |
| `runtime/model_router.py` | both `import litellm` sites and the capability rung now go through `ensure_litellm`; the per-call `latency` block rides every ledger row, `get_last_usage()` and the `model_routed` trace event |
| `runtime/model_capabilities.py` | `_from_litellm` goes through `ensure_litellm` |
| `runtime/__init__.py` | fires the preload at process start (`_start_litellm_preload()`), and exports `preload_state` / `start_preload` lazily |
| `runtime/startup_audit.py` | the W1.4 scanner: **34 hits, 0 unclassified** |
| `runtime/test_latency_pins.py` | 60 tests, in-package (see §0 of that file for why bare pytest misses it) |

### 2. THE FINDING THAT MATTERS: `sys.modules` is not a readiness signal

**This is the most valuable thing the round produced and it was not in the
brief.** CPython inserts a module into `sys.modules` **BEFORE** executing its
body (so circular imports work). A raw `sys.modules.get("litellm")` during a
concurrent import returns a **half-built module**. Measured against real
litellm on this host, 66 ms into a preload:

| probe | value |
|---|---|
| `'litellm' in sys.modules` | **True** |
| `litellm.completion` callable | **False** |
| `litellm.*` submodules loaded | **3** of 749 |

`importlib._bootstrap._lock_unlock_module`'s own docstring is *"used to ensure
a module is completely initialized, in the event it is being imported by
another thread"* — so `importlib.import_module` is the interpreter's answer
and a dict read is not. **This module's first version read `sys.modules`
directly on its fast path and was wrong.** The fix routes every access
through `importlib.import_module`; `module_present()` exists so the mistake
cannot be reintroduced silently, and its docstring says what it does and does
not mean.

A race that hands a first prompt a half-initialised provider is far worse
than 18 seconds, so this is recorded as a hazard class rather than a nit.

### 3. Two more defects the round found IN ITS OWN CODE, and the measurements that caught them

Both were caught by measuring rather than reading, and both are the reason
the pins exist.

**(a) One lock for two jobs made the diagnostics block for 14.6 s.** The
first version held a single `_LOCK` across the whole import, so
`preload_state()` — the function whose entire job is to report on a preload —
blocked for the full import duration, and a second `start_preload()` blocked
behind the first. There are now two locks with two jobs: `_IMPORT_LOCK`
(held only across the import, serialises our own imports) and `_LOCK` (held
only for short state reads/writes, never across the import). Pinned by
`test_the_two_locks_are_not_one_lock` and
`test_preload_state_returns_while_an_import_is_in_flight` (0.07 ms measured).

**(b) `start_preload` called the (correctly blocking) readiness check, so
"kick off a background preload" became a synchronous 6-second import** and
the race it was written to set up never happened — the probe reported
`background preload started: False` and `import_calls: 0`. The
already-loaded short-circuit was deleted: a daemon thread that returns in
microseconds when there is nothing to do is strictly cheaper than a kickoff
that can block for fifteen seconds.

**(c) `"auto"` was in `_TRUTHY`, so the documented default value enabled the
preload UNCONDITIONALLY.** `_preload_mode()` tested the truthy set before the
`auto` branch, so `NEO_PRELOAD_LITELLM=auto` resolved to `on` and silently
defeated the interactive and not-a-test-session gates that make `auto` safe.
Found by `test_auto_mode_refuses_inside_a_test_session`.

**(d) A synthetic module cannot test the blocking guarantee.** A hand-made
`types.ModuleType` in `sys.modules` is never "in progress" as far as
`importlib` is concerned, so `import_module` returns it immediately and the
race test passes for the wrong reason. The pins write a REAL module to a temp
dir and import it for real, because that is the only way to hold CPython's
import lock. (The first draft of these tests got this wrong and was corrected.)

### 4. The thread-safety contract, stated as a contract

* `ensure_litellm()` is the **only** place any `runtime/` code imports
  litellm. `test_no_module_in_runtime_imports_litellm_directly` walks the AST
  of every `runtime/*.py` and fails on any `import litellm` / `from litellm`.
* A background preload and a foreground call both execute the import while
  holding `_IMPORT_LOCK`, and both go through `importlib.import_module`, which
  blocks on the interpreter's per-module lock. A caller arriving mid-preload
  therefore waits and returns the **completed** module.
* **Measured race, real litellm, real preload:** foreground call
  **blocked 9.28 s**, then got a module with `completion` callable and 749
  submodules, identical to `sys.modules['litellm']`. `waited_calls: 1`,
  `import_calls: 1` — the module was imported once despite two callers racing.
* A preload that **fails** is recorded (`state: failed`, `error`, `error_kind`)
  and re-raised to the caller that needs it; the NEXT call retries the import
  through the same function. The lazy path *is* `ensure_litellm` with no
  preload, so "degrades to the lazy path" is a property of the design rather
  than a second code path. Pinned both ways, with a working import as the
  control.
* `auto` mode requires an interactive process that is **not** a test session.
  The test-runner exclusion is deliberate and measured: an 18-second background
  import sharing a process with tests is the exact host-load-flake class this
  file records in four separate rounds. `NEO_PRELOAD_LITELLM=on` overrides it.
* `start_preload` is idempotent on `_STATE.requested`, not `_STATE.started` —
  the latter is only set once the thread reaches the import, so between
  "decided" and "started" two concurrent callers would both pass it.

### 5. The latency receipt, and every `unavailable` path

Rides every ledger row, every `get_last_usage()` record, and the
`model_routed` trace event, as a nested `latency` block plus **flat** keys
(so a JSONL/`jq`/spreadsheet reader gets the numbers without knowing this
module's shape). Every numeric field is `(value, state, reason)`.

| field | unavailable when | reason string |
|---|---|---|
| `ttft` | streaming disabled | `stream_disabled` — and `streamed: false`. **Never 0.0**: a zero TTFT reads as "the provider answered instantly", which is a different claim |
| `ttft` | streamed but no delta arrived | `no_delta_observed_before_completion` |
| `overhead` | never dialed | `call_not_dialed` |
| `overhead` / `provider` | mock lane | `mock_lane_has_no_gateway_overhead` / `mock_provider_has_no_network_dial` |
| `provider` | did not finish | `call_did_not_finish` |
| `generation` | not streamed, or no TTFT | `stream_disabled` / `no_first_token_measured` |
| `input_tokens_per_s` | provider reported no usage | `provider_reported_no_usage` / `no_usage` |
| `output_tokens_per_s` | no generation window | `no_generation_window_measured` |

Two denominator rules that matter:

* **Input rate is over the PROVIDER window; output rate is over the
  GENERATION window** (provider minus TTFT). Dividing by the wrong window is
  how a throughput number becomes decorative. When there is no generation
  window the output rate says so rather than silently falling back.
* **The mock lane reports no provider time and no gateway overhead.** The
  scripted provider computes locally; calling that "provider time" would let
  a mock ledger be read as throughput evidence. `dial_kind: "mock"` and
  `tokens_estimated: true` are on every such row.

`overhead` vs `provider` is a real honesty fix: `started` used to be taken
*before* the capability-screen rung, so an 18-second import was folded into
`elapsed_s` and reported as if the **provider** had been slow. That is the
direction that misattributes our own defect to somebody else's endpoint.

**Percentiles** (`latency.percentiles` / `latency.latency_window()`):
nearest-rank, never interpolating, so a reported percentile is a value that
was actually measured. `None` samples are **skipped, not counted as zero** —
a call whose TTFT was unavailable did not have a fast TTFT, and counting the
gaps as zero would drag p50 to 0.0. Every block reports `samples`,
`observed`, `skipped`, and `provisional`; below
`MIN_PERCENTILE_SAMPLES = 20` the reason says so in words. A window with
nothing measured returns `state: "unavailable"` with a reason, never an
empty-looking zero.

**Percentile window sizes actually reported:** every run in this round used
**n = 3**, i.e. `provisional: true` on every block. That is the honest state
of the evidence and it is why the DoD is stated as a measured range rather
than a p95.

### 6. `python -m runtime.latency` and the diagnostics surface (T4)

`preload_state()` is the receipt `cli/doctor.py` should render. It is
**non-blocking by construction** (0.07 ms measured while an import is in
flight) and reports `state`, `present`, `in_flight`, `ok`, `error`,
`error_kind`, `duration_s`, `waited_s`, `waited_calls`, `import_calls`,
`local_cost_map`, `interactive`, `under_test_runner`, `eligible`, `env`.
`latency_report()` bundles the import half and the call half side by side,
because the useful question is the ratio: how much of the first call's wait
was the import, and how many calls have been free of it since.

`module_initialised()` / `probe_initialised()` **block** and are named for
it. `preload_state()` deliberately does not use them.

### 7. THE MEASUREMENTS, before and after

Cold subprocess, median / min / max over 5 runs, this host:

| probe | before | after |
|---|---|---|
| `import cli.main` (≈ `neo --version`) | 0.68 s | **0.70 s** (0.66 / 0.80) — unchanged, as designed |
| `import runtime.model_router` | 0.19 s, litellm absent | **0.19 s** (0.18 / 0.22), litellm absent |
| `import litellm` | 13.09 s claimed; 17.6–22.2 s measured early | **9.76 s** (8.94 / 10.58) median; **6.1–22.5 s** across the session |

**Startup → first token, honestly split into the two numbers that phrase
covers.** Measured through the real
`call_model(stream=True, on_delta=...)` path; model = the offline scripted
provider, so **provider network time is excluded** and no live lane is claimed.

* **ENTER → first token, session already open** (what a daily user waits for
  every day): **0.0060 s median, 0.0070 s max, n=3.** The preload makes the
  import free for every call after the first turn. **Target <2.0 s: met, by
  ~300×.**
* **Cold process → provider library ready**: **9.76 s median, 6.1–22.5 s
  range.** **Target <2.0 s: NOT met, and it cannot be met by strategy A.**

The preload converts `think + import` into `max(think, import)`. The measured
sweep (ready_at vs simulated user think time):

| think | no preload | preload | hidden |
|---|---|---|---|
| 0 s | 7.65 s | 7.19 s | 0.46 s |
| 2 s | 9.58 s | 7.70 s | 1.88 s |
| 5 s | 11.01 s | 7.45 s | 3.56 s |
| 10 s | 18.53 s | 10.05 s | 8.48 s |
| 20 s | 28.16 s | 20.07 s | 8.09 s |

**The honest limit:** the preload hides the import to the extent the user's
think time covers it, and for an **instant scripted prompt there is no
overlap to be had**. That is arithmetic, not an implementation gap. Closing it
needs the import itself to be cheaper — strategy C (vendoring, the brief's
last resort), or a litellm that lazily imports its provider graph.

**A finding about the offline lane, and it is why the preload has its own
test:** `_cache_capability` sets `provider = "mock"` on the mock lane, and
`"mock"` is in `model_capabilities.SYNTHETIC_PROVIDERS`, which `_from_litellm`
refuses **before** the import. So **no offline test in this repository can
regress the 18-second import** — the mock lane reports ~0.15 s whether or not
a preload ran. The subprocess pins exist for exactly that reason.

### 7a. THE EXIT GATE ITSELF: rung #9 does not cover this defect, and I can prove it

`python -m evals.run --suite trust-ladder` **does not exist** in this tree
(`evals/run.py` registers `auto | combined | daily-driver | prompt-regression`),
and `evals/gates/P0.py` already records that as a known gap. The ladder's real
entry point is `python -m evals.trust_ladder`, and **rung #9 is this round's
gate** ("Fast enough to use daily", owner `T1 / P1.1 + T3 / P1.3`).

**Rung #9 is a conjunction of FOUR budgets, and mine is green:**

| rung #9 metric | measured | budget | verdict |
|---|---|---|---|
| **`startup_to_first_token`** | **1125 ms** median, min 868, max 1426, n=5 | 2000 ms | **PASS — mine** |
| `tui_frame_cost` | 5 ms median, min 5, max 12, n=5 | — | pass |
| `retrieval_cold_then_warm` | **55 421 ms** | 1000 ms | **FAIL — T1's `harness/retrieval.py::_SKIP_DIRS`** |
| `retrieval_walk_dirs_opened` | 157 dirs | — | context |

The rung is therefore red on **T1's retrieval skip-list**, and the probe
itself attributes it correctly: *"the real repo opened 157 directories. Cause:
the skip list (45 entries) does not cover this repository's large output
directories. This is a SKIP-LIST defect, not a slow machine."* The control arm
(1500 noise files, all inside covered directories) walks in **8 ms** vs
**55 421 ms** — **6985×** on the same machine.

**The important part: my budget was already green and this round did not move
it, because the gate's probe cannot see the defect it is supposed to guard.**

`evals/trust_ladder_rungs.py::_fresh_startup_ms` times
`import cli.interactive, cli.runview, shared.agent_contracts` in a fresh
interpreter. Measured, that path **does not import `runtime` at all**:

```
runtime loaded: False | litellm loaded: False
```

so my preload never fires on it, and the 18-second model-call import is
outside the measurement entirely. A/B, 5 fresh interpreters each:

| arm | median | min | max |
|---|---|---|---|
| preload OFF (pre-round behaviour) | 703.2 ms | 690.7 | 720.1 |
| preload ON (this round) | 690.1 ms | 676.4 | 734.3 |

Both comfortably under the 2000 ms budget; the 13 ms difference is noise.
The probe's own docstring is honest that it measures *"the import graph the
user waits behind"* and not a provider call — but on this tree the user waits
behind that import graph *plus* the litellm import on the first prompt, and
only the first is measured.

**So: G1 rung #9 is red, not because of this round, and its green budget does
not certify the fix.** Filed to T5 as request 7 in §11. It is also the reason
`runtime/test_latency_pins.py` drives the preload through **subprocesses**: the
existing probe architecture cannot see it.

### 8. W1.3 — the turn cap. The guard-ordering answer, with evidence.

**Raising `agent_max_turns` from 25 to ≥50 CANNOT make the budget cap fire on
a later turn.** Established by reading both turn loops, not by assumption:

```python
for turn in range(1, max_turns + 1):        # the CEILING is the loop bound
    if time.time() >= deadline:        -> timeout
    if total_cost_usd >= budget_cap_usd:    -> failed     # BUDGET, every turn
    ... one model call ...                                # the work
```

`harness/agent_kernel/strategy.py:503-520` and `harness/agent_loop.py:1707-1717`
have this identical shape. Three pieces of evidence, each pinned:

1. The budget comparison is **inside** the loop body and there is **no**
   `if turn` / `turn <` / `turn ==` guard between the loop header and it, so
   it is evaluated on turn 50 exactly as on turn 1.
2. The budget check **precedes** the turn's work (`_begin_turn`), so a turn is
   never paid for after the cap.
3. `runtime/budget_governor.py` does not read `max_turns`,
   `agent_max_turns` or `max_step_turns` at all — the ceiling cannot
   participate in the cap decision. Driven directly: 8 charges of $0.25
   against a $2.00 cap reach the cap on **charge 8**, and that number is
   independent of any ceiling.

There is a guard **one level tighter** than the per-turn check:
`BudgetGovernor.authorize_call` prices the NEXT call **before the request is
built** (installed at the dial by `runtime/provider_gateway.py` and around
`ModelClient.call` by `harness/core.py::_bind_budget_guard`), so the enforced
bound is `cap + the price of the final call`, not `cap + a whole turn`. A
refused call leaves **no** reservation behind — a stuck reservation would
keep refusing every later call too (pinned).

**The worst-case cost at the new ceiling**, priced through the one price
authority (`runtime.model_capabilities.MODEL_PRICES`), for a task that
consumes every turn it is allowed, at a mean 17 000 prompt + 800 completion
tokens per turn:

| tier | model | price (in/out per 1M) | 25 turns | **50 turns** |
|---|---|---|---|---|
| easy | `gpt-4o-mini` | 0.15 / 0.60 | $0.076 | **$0.152** |
| hard | `claude-3-5-sonnet-20241022` | 3.00 / 15.00 | $1.575 | **$3.150** |

**"A task that hits the new ceiling at frontier prices costs approximately
$3.15."** And that figure is **unreachable with the shipped defaults**:
`budget_cap_usd = 2.0` stops the run first, at the same turn it would have
at a ceiling of 25. So raising the ceiling is a **capability** change, not a
cost change — *provided the budget cap stays where it is*. The failure mode
to guard against is an operator raising `agent_max_turns` **and**
`budget_cap_usd` in the same change; that is a deliberate 2× money decision.
The pin `test_the_worst_case_cost_at_the_new_ceiling_is_reported_not_asserted_away`
asserts `$3.15 > $2.00` so the "budget fires first" claim fails loudly if the
numbers ever move under it.

**`spent_usd()` is a maximum, re-pinned — and the obvious statement of the
property is wrong.** The governor's OWN charges *are* summed (20 × $0.10 =
$2.00; pretending otherwise would under-report spend). What must be a maximum
is the combination of the governor's total and the bound `spend_source`,
because they are two views of the **same** money: the governor prices the
calls it saw, and `ModelClient`'s total already includes a provider
fallback's earlier charges. Adding them double-counts, and a cap that fires
late is worse than one that fires early. Pinned both ways, including the
control that two views reporting the *same* $1.00 read as $1.00 and not
$2.00, and that a `spend_source` which **raises** is ignored rather than
being allowed to take the cap down with it.

**Quota vs budget, re-pinned.** Quota is classified **first** and
`provider_fault=False`, `retryable=False`, `replayable=False` — an empty
account is not a provider fault, and counting it as one trips the breaker and
fails over to a second target on the **same billing account**. The two
shapes are not confusable by status code (several providers return quota as
a 429), and `provider_resilience.QUOTA_MARKERS` is asserted to **be** the
governor's set (identity, not equality) so the two classifiers cannot drift.
`QuotaExhausted` is a `RuntimeError` and specifically **not** a
`PermissionError` or a `BudgetRefused`, so the gateway's fail-closed paths
cannot absorb a refusal and turn it into a normal-looking run.

### 9. W1.4 — every expensive module-level operation, classified

`python -m runtime.startup_audit` → **34 hits, 0 unclassified.** Four
detectors: module-level **calls**, **comprehensions**, **large literals**, and
**cross-module imports**. The fourth detector was added *after* measuring,
because the shape that actually cost the most was invisible to the first
three:

| site | measured | verdict |
|---|---|---|
| `roles.py:19` `from harness.agent_kernel import ...` | **318 ms** (vs **0.029 ms** for the comprehension it feeds) | `deferred` — **the single highest-value follow-up in this audit** |
| `worker.py:57,58`, `orchestration.py:33,63,64`, `orchestration_worker.py:14,30`, `subagents.py:31`, `roles.py:20`, `ablation.py:78` — the same 318–361 ms import | | `deferred` — on the **worker start** path, not the per-call path |
| `model_capabilities.py:1458` `_BUILTIN_EFFORT_KNOBS` (3 entries) | 0.0 ms | `constant` |
| `model_capabilities.py:57` `__all__` (71 strings) | 0.0 ms | `constant` |
| `privacy_policy.py:125` `_CLASSES` (6 entries) | 0.0 ms | `constant` |
| `multirepo_tasks.py:97` `CACHE_ROOT` `os.environ.get` | 0.0 ms | `constant` |
| 16 × `shared.*` imports | 3.3–118.4 ms, all **under** the 150 ms floor | `constant` |

Measured first-party import costs (median of 3 cold subprocesses) are recorded
as a literal in `runtime/startup_audit.py::FIRST_PARTY_IMPORT_MS`, so a new
cross-module import is classified **from its measured cost** the moment it is
measured rather than becoming an open audit item. The floor is 150 ms,
chosen against the two things it must be compared to: the 0.70 s
`neo --version` this project treats as healthy, and the 6–22 s litellm
import. Below the floor, deferring an import trades a real correctness
boundary (a redaction authority, a serialized type contract) for a latency
nobody can feel.

**The 318 ms was not fixed, and here is why.** `_TOOL_SPECS` is module-level
public state read by other modules, so making it lazy is a shape change to
another owner's file. It is recorded, not done. Note also that nothing under
`model_router` / `provider_gateway` imports `runtime.roles`, so it is **not**
on the per-call path — a 50-way fan-out pays it 50 times in PARALLEL, which
is why it is still worth someone's time.

### 10. Not implemented, stated plainly

* **The cold-process number is not under 2.0 s and strategy A cannot make it
  so** (§7). Closing it needs strategy C.
* **No live-provider lane was run.** Every provider-shaped number here is the
  offline scripted provider. A real TTFT, a real tokens/sec, and a real
  overhead/provider split are **unmeasured** — the mechanism that would
  produce them is in place and its correctness is pinned.
* **The kernel's `ModelGateway` path still has no per-call budget
  pre-check** (pre-existing, R2-14 §5); it is covered by the per-turn check.
  With a 2× ceiling that path's per-turn overshoot is 2× as large.
* **`local_cost_map` is off by default** and worth ~0.3 s warm, not the 5.9 s
  an early single measurement suggested. Offered for reproducibility, not
  latency.
* **No percentile is reported from more than 3 samples in this round.** Every
  block is `provisional: true`. A p95 needs a real window; this one does not
  have one yet.
* **`startup_audit.py` does not scan `harness/`, `cli/`, `shared/` or
  `execution/`** — `runtime/` only, per the scope boundary. The
  `FIRST_PARTY_PACKAGES` set is already first-party-wide, so pointing it at
  another package is a one-line change for whoever owns it.
* **No `INTERFACES.md` entry was written** (not this round's file). The
  contract additions are in §11.

### 11. Cross-terminal requests

1. **T4 (`cli/doctor.py`) — the latency rail.** Render
   `runtime.latency.preload_state()` (`state`, `in_flight`, `present`, `ok`,
   `error`, `duration_s`, `waited_s`, `waited_calls`, `eligible`,
   `under_test_runner`, `env`) and the per-call block's
   `latency_ttft_s` / `latency_ttft_state` / `latency_ttft_reason` /
   `latency_overhead_s` / `latency_provider_s` / `latency_import_wait_s`.
   **Show the STATE next to the value**: a `null` TTFT that renders as a blank
   cell is indistinguishable from a 0 ms one, and that is the exact confusion
   the block exists to prevent. `preload_state()` never blocks (0.07 ms
   measured), so it is safe to call from a live render loop.
2. **T4 (`cli/tui.py` / `cli/tracelog.py`) — the new flat trace keys.**
   `latency_ttft_s`, `latency_ttft_state`, `latency_ttft_reason`,
   `latency_duration_s`, `latency_overhead_s`, `latency_overhead_state`,
   `latency_provider_s`, `latency_provider_state`, `latency_import_wait_s`,
   `latency_input_tokens_per_s`, `latency_output_tokens_per_s` ride the
   existing `model_routed` event. Additive, unclassified.
3. **T5 — CI wiring for `runtime/test_latency_pins.py`.**
   `pyproject.toml` sets `testpaths = ["tests"]`, so **a bare
   `python -m pytest` collects NONE of these 60 tests.** Command:
   `python -m pytest runtime/test_latency_pins.py -q` (60 passed, 23 s,
   host-only — no Docker, no network, no credential). This is the third
   round to file the same request for a `runtime/`-local suite; the general
   fix is a selection entry that globs `runtime/test_*.py`.
4. **T5 — the stale pin in `tests/test_scheduler_integration.py:348`,
   re-confirmed this round.** `test_worker_exception_output_redacts_credentials`
   asserts `"[REDACTED]" in rendered`; `_safe_exception_text` delegates to
   `shared.security`, whose placeholder is `[REDACTED_SECRET]`. **Proven not
   mine**: `runtime/worker.py` was not opened in this session, and the
   security property still holds (`sentinel not in rendered` → rendered is
   `api_key=[REDACTED_SECRET] Bearer [REDACTED_SECRET]`). The fix is
   unchanged from P0/W1 §6.1: change the second assertion to
   `"[REDACTED_SECRET] in rendered"`. **This round adds four pins T5 should
   include**, none currently covered anywhere: an ANSI-split key
   (`"sk-abc\x1b[0mdef123456789"` → `"key [REDACTED_SECRET]"`), a
   URL-userinfo key, a query-string key, and a monkeypatched-raising redactor
   → `(withheld: ...)`.
5. **T1 — the turn-cap pins, for `harness/`.** The ordering property is now
   pinned **from `runtime/` by reading `harness/` sources read-only**
   (`test_the_budget_check_is_inside_the_turn_loop_and_unguarded_by_the_index`,
   `..._precedes_the_turns_work_in_the_loop_body`, and the `agent_loop.py`
   twin). Those pins are structural and will fail if the ordering moves —
   which is the point. What T1 should add on the harness side: a
   behavioural test that a run's total spend is bounded by `budget_cap_usd` at
   **both** 25 and 50 turns, so the relationship cannot drift silently. Also
   worth recording: the kernel's `ModelGateway` path has no per-call budget
   check (R2-14 §5), so with a 2× ceiling its per-turn overshoot is 2× as
   large. If T1 wants that closed, it is `ModelGateway` → `authorize_call`.
6. **Whoever owns `runtime/roles.py`'s import** — the 318 ms
   `from harness.agent_kernel import ...` is the highest-value startup item in
   the audit (§9). `roles.py` is T3's, but `_TOOL_SPECS` is read by
   `harness.agent_kernel` itself, so the fix is a joint shape change: build
   the alias index lazily behind a function, and have the two readers call
   it.
7. **T5 (`evals/trust_ladder_rungs.py`) — rung #9 has a coverage hole this
   round proved, and it is the reason G1 can go green while a user still waits
   18 seconds.** `_fresh_startup_ms` times
   `import cli.interactive, cli.runview, shared.agent_contracts`, and that path
   **does not import `runtime` at all** (measured: `runtime loaded: False`,
   `litellm loaded: False`). So the `startup_to_first_token` budget — the one
   rung #9 attributes to `T3 / P1.3 (import path)` — cannot observe the
   18-second model-call import, and passes at 703 ms whether or not the
   preload exists. **A second timing that reaches the model-call path is
   needed**, and the one honest way to reach it without a credential is
   `runtime.latency.preload_state()` + a fresh-interpreter import of
   `runtime.model_router` followed by `runtime.latency.ensure_litellm()`,
   reported as its own metric with its own window. Please keep it separate
   from `startup_to_first_token` rather than folding it in: they measure
   different populations (the CLI's import graph vs the provider library's),
   and averaging them would produce a number that describes neither.
   Everything else about the existing probe is right and should be kept —
   in particular the refusal to print a p95 below `MIN_SAMPLES_FOR_P95` and
   the `available: False`-is-not-a-zero rule, which `runtime/latency.py`
   follows deliberately.

### 12. Verification actually run (this tree, `PYTHONIOENCODING=utf-8`, `-p no:randomly`)

1. `python -m pytest runtime/test_latency_pins.py -q` → **60 passed** (23.1 s).
2. `tests/test_model_router.py tests/test_streaming_ui.py` → **151 passed**.
3. `tests/test_ceiling_r2_14_budget_quota.py tests/test_provider_smoke.py` →
   **60 passed, 5 skipped**. The 5 skips are the no-credential provider
   smokes — **not passes**, and not claimed.
4. `tests/test_prompt_cache_cost.py tests/test_ceiling_r2_13_capability_routing.py`
   → **92 passed**.
5. `python -m evals.run --check` → **14/14 CLEAN, exit 0**. No prompt changed,
   so this is a no-regression receipt and **not** a claim about model quality.
6. `python -m ruff check runtime` → **6 findings, all pre-existing** — 3 in
   `abuse.py` and 3 in `multirepo_tasks.py`, both files this round did not
   open. **Proven, not assumed**: `git diff -- runtime/abuse.py` is a prior
   round's f-string cleanup, not this round's. `ruff --fix` was **scoped to
   this round's own files** (the W1 lesson in P0/W1 §7: an unscoped
   `--fix` stripped two deliberate `# noqa` directives from `abuse.py` in an
   earlier round). All 5 owned/created files → **All checks passed!**
7. `python -m compileall -q runtime` → **exit 0**.
8. `git diff --check` on the four edited files → **exit 0** (LF→CRLF warnings
   only, which is this tree's pre-existing line-ending state).
9. Wider regression selection (`test_model_router`, `test_streaming_ui`,
   `test_streaming_live`, `test_ceiling_r2_14_budget_quota`,
   `test_ceiling_r2_13_capability_routing`, `test_prompt_cache_cost`,
   `test_provider_smoke`, `test_import_smoke`) → **368 passed, 5 skipped**.
10. Neighbouring selection (`test_ceiling14_resilience`,
    `test_scheduler_integration`, `test_config_trace_state`, `test_tracing`,
    `test_ceiling_r2_15_trust`, `test_ceiling_r2_09_scale`) → **225 passed,
    1 skipped, 1 failed**. The one red is §11.4 — the stale pin filed in
    P0/W1 §6.1, **proven not this round's** and **not counted as a pass**.
11. `python -m evals.trust_ladder --measured-only` → **`MEASURED_RERED`**,
    counts `pass=0, fail=3, blocked=0, not_implemented=0`. Rung **#9 FAIL**,
    and its own breakdown is the receipt: `startup_to_first_token` **1125 ms**
    against a 2000 ms budget (**this round's budget, green**),
    `retrieval_cold_then_warm` **55 421 ms** against 1000 ms (**T1's
    `harness/retrieval.py::_SKIP_DIRS`**, with the probe's own control arm
    proving it is a skip-list defect rather than a slow host). Rung #8 FAIL is
    T1's (`agent_max_turns` is still 25 in `harness/config.py`; the probe
    requires ≥50 — which is the interface this round's §8 pins are written
    against). Rung #10 FAIL is T4's projection chain. **No rung failure is
    this round's, and none of them is a pass either.** See §7a.
12. **The 5 in-package runtime pin modules together (150 tests)** →
    **150 passed, then 149 passed / 1 failed, then 148 / 1 — the reds moved.**
    They moved because **two other terminals were writing the two files those
    pins read, during the runs**, and this round measured that rather than
    re-running until green:

    | when | observed | whose write |
    |---|---|---|
    | 02:5x | 150 passed | — |
    | 03:18 | `SyntaxError` at `execution/sandbox.py:2557` | T2 mid-write (mtime 03:18:34) |
    | 03:22 | `NameError: name 'Sequence' is not defined` at `execution/verify.py:1394` (`Optional[Sequence[str]]` used in a module-level annotation with no `Sequence` import) | T2 mid-write (mtime 03:24:26) |
    | 03:26 | a *different* pin in the same module red | T1's `harness/agent_loop.py` (mtime 03:25:20) |

    `runtime/test_latency_pins.py` — **this round's 60 tests — passed in every
    one of those runs**, standalone and with `pytest-randomly` on. The
    instability is entirely in `runtime/test_boundary_signature_pins.py` (a
    prior round's module) reading `harness/agent_loop.py` and
    `execution/verify.py`, and it is the shared-dirty-tree hazard this file
    has now recorded in five separate rounds. **Neither file was repaired
    here** — the shared-tree rule is that a half-written file in another
    module belongs to its owner, and re-running until green would have turned
    a measurement into a claim.

---

## P0/W1 T3 — network egress, the money ledger, and the eight honesty invariants (2026-10-01)

**NEW `runtime/redaction.py` and NEW `runtime/invariants.py`. Edited:
`runtime/budget_governor.py`, `runtime/model_capabilities.py`,
`runtime/model_router.py`, `runtime/checkpoint.py`, `runtime/worker.py`,
`runtime/offline_mode.py`.** `runtime/abuse.py` was touched by a
`ruff --fix` accident and restored byte-for-byte (§7). Nothing outside
`runtime/**` was edited; tests are T5's and there are two filed requests (§6).
**The litellm import was measured and NOT fixed** — that is P1.4's, gated on
the numbers in §5.

### 0. The two authorities this round created

| module | owns | why it is a module and not a helper |
|---|---|---|
| `runtime/redaction.py` | **the ONE boundary where provider/network text becomes a product value** | it is the only place a credential can enter an artifact; §1 |
| `runtime/invariants.py` | **the eight honesty rules as failing-if-broken checks** | a comment is not evidence; `python -m runtime.invariants` |

### 1. `runtime/redaction.py` — four rules, and the ORDER is the design

1. **Strip ANSI escapes BEFORE redacting.** An escape can split a secret into
   visually-contiguous bytes. Measured on this tree:
   `"key sk-abc\x1b[0mdef123456789"` — `redact_text` alone returns it
   **unchanged**; strip-then-redact returns `"key [REDACTED_SECRET]"`. This is
   the ordering the engineering doctrine names, and `cli/ui.py::strip_ansi`
   is the shipped example of the wrong order. **If you add a redactor, strip
   first.**
2. **Delegate to `shared.security`.** This module implements NO pattern of its
   own — two redactors is two answers to "what is a secret". It adds the
   ordering, the length cap, and the fail-closed wrapper that the shared
   redactor does not provide.
3. **Cap the length LAST.** 9000 chars in → 512 out, with `[truncated]`
   marked. A cap applied before redaction is what lets a truncation hide a
   secret's tail.
4. **Fail CLOSED.** A broken or unavailable redactor yields
   `(withheld: <reason>)`. **Never `None` and never `""`** — both read as
   "no key was involved", which is the opposite of the truth. Measured:
   `redact_text` monkeypatched to raise → `"(withheld: redaction unavailable
   for provider detail: RuntimeError)"`.

**The class is never redacted.** `classify_provider_failure` owns *what
happened* and `classify_retry` owns *what to do next*; both are vocabulary,
not provider text. `provider_failure_detail()` makes "classify first, redact
second" impossible to get wrong by requiring `kind=` as a keyword argument.

### 2. What was wired to it — and the root cause under three of them

`runtime/checkpoint.py` **had a redacting writer** (`_append_jsonl_durable`,
whose own docstring says redaction "is not optional here") wired only to
`compactions.jsonl` and the turn pre-image ledger. `TaskCheckpoint.log_event`
went out through the RAW `fsutil.append_jsonl`. So the worker journal — the
artifact holding approval errors and a quota block — was the one durable
runtime artifact that could persist a credential verbatim. **One-line fix,
one-line cause.** Every other finding below is downstream of "safety rested on
each caller remembering".

| site | was | now |
|---|---|---|
| `budget_governor` ×6 | `detail=str(exc)[:500]` → `budget.json`, `checkpoint.json`, worker journal (all unredacted at the writer) | `_safe_detail()` → redacted + 3 honest flags |
| `checkpoint.log_event` | raw `append_jsonl` | `redact_secrets`, fail-closed into the journal |
| `model_router._safe_error` | 4 own regexes, no ANSI strip, no fail-closed | delegates |
| `worker._safe_exception_text` | **truncated at 500 BEFORE redacting**, no URL coverage, no known-secret | delegates |
| `worker` approval ×2 | `str(exc)` straight to the journal | redacted before journal AND trace |
| `model_router` difficulty fallback | `f"{type(exc).__name__}: {exc}"[:200]` → unredacted ledger | redacted |
| `offline_mode` drain failure | `f"..."[:200]` → raw `json.dumps` queue | redacted |

**`ProviderFailure` gained `detail_withheld` / `detail_redacted` /
`detail_truncated`.** "The provider said nothing", "we redacted it" and "we
cut it" used to be the same empty string. They are additive keys on an
existing receipt; `as_dict()` only grew.

**Two redactors were deleted, not merged.** The worker's was the weaker of the
two in a way that mattered: slicing to 500 chars *before* redacting means a
key straddling the cut is no longer shape-matchable. Both now read one
authority.

### 3. TWO REAL PRICING DEFECTS FOUND BY THE HONESTY CHECKS

**This is the round's most valuable output, and neither was in the prompt.**

**(a) An operator-declared rate was invisible for any router model.**
`register_capability` keyed the registry on the model string *as written*;
`lookup_capability` reduced a `provider/` prefix *before* consulting it. For
a bring-your-own router — whose model string is normally prefixed, because
that is litellm's own routing form — the two could never match:

```
register_capability({"provider":"tokenrouter","model":"z-ai/glm-5.3-free",
                     "input_cost_per_million":0.6,"output_cost_per_million":2.2})
estimate_cost("z-ai/glm-5.3-free", 1_000_000, 1_000_000)
  BEFORE -> CostEstimate(cost_usd=0.0, price_state='unpriced', priced=False)
  AFTER  -> CostEstimate(cost_usd=2.80, price_state='priced',   priced=True)
```

That is the direction the honesty rules name — a report that under-counts cost
in the direction that **spends money** — and it silently discarded the
operator's own explicit instruction to pay. `declared_rates()` was blind to it
too, so R2-14's budget pre-check priced the call at nothing. Fixed in
`_registered_by_model` (:731) and `lookup_capability` (:739, raw key tried
first, reduced key second), and `unregister_capability` (:618) now accepts
either spelling so a prefixed registration is not unwithdrawable.

**(b) The per-call budget pre-check ignored in-flight reservations.**
`authorize_call` computed `remaining = cap - spent` while the public
`remaining_usd()` computed `cap - spent - reserved`. Two formulas for one
question, disagreeing by exactly the reservations that exist to stop a burst.
Measured on a 0.10 cap with 0.05 reserved: a further **0.06 call was
ALLOWED** (1.1× the cap reserved at once) and `exhausted` only flipped
afterwards. `authorize_call` now reads `self.remaining_usd()` (:997). The
bound is now `max`-shaped here too and can only fire **earlier**.

### 4. `python -m runtime.invariants` — 9/9 hold, and 13/13 mutations detected

Eight invariants plus the structural-predictor guard. `--json` for a machine
receipt, `--only NAME` to scope, **exit 2 on any violation**, no warn tier.

| invariant | violation it prevents |
|---|---|
| `unpriced_is_never_zero` | an absent price row reported as `$0` and picked as cheapest |
| `unknown_capability_is_never_false` | `False` refuses to route the entire world |
| `spent_usd_is_a_maximum` | a sum fires the cap **later** than intended |
| `effort_parameters_empty_unless_sent` | "I set it to max" silently becoming "high" |
| `unsupported_effort_is_never_clamped` | the same, from the other direction |
| `a_budget_refusal_is_sticky` | a refused call retrying itself |
| `unreceipted_calls_is_reported_honestly` | a missing ledger reading as "we checked, it's zero" |
| `cache_status_separates_unreported_from_miss` | a provider that never reports usage reading as a 0% hit rate |
| `structural_predictor_is_still_unshipped` | a predictor that won on **one** hard label shipping silently |

**Every check carries a CONTROL arm.** Asserting "never clamps" is
satisfiable by a registry that sends nothing at all, so `max`→`unsupported_level`
is paired with `high`→`reasoning_effort=high`; "the gate refuses unpriced" is
paired with `allow_unpriced=True` letting the same model through; "unknown is
None" is paired with a declared `False` staying reachable. **A result with no
observations is itself a FAILURE** — a check that observed nothing has
verified nothing, which is the one thing this repo records as worse than no
test. A check that RAISES is reported as a failure, never as a pass.

**Measured sensitivity** (out-of-tree monkeypatch probe, no source edited):
**13/13 mutations detected.** unpriced→free; price state dropped from
`to_dict()`; `None`→`False`; `spent_usd` summed; a raising bound disabling the
cap; a non-sent plan gaining a parameter; `max` clamped to `high`; a supported
level sending nothing; a refusal lapsing; `unreported`→`miss`; a lying
`cache_tokens_from_usage`; `auto` switching to structural; structural becoming
unreachable.

**The structural guard's shape is worth keeping.** It checks FOUR things, not
one: the calibration file `runtime/difficulty_structural_calibration.json` does
not exist; `_structural_calibrated()` returns nothing; the default path
resolves to the **incumbent**; and `difficulty_features="structural"` still
resolves to the **challenger**. The last two together are what stop the check
passing vacuously — if both arms returned the same estimator the switch would
be inert and "unshipped" would be indistinguishable from "shipped".

### 5. litellm import + first-call baseline — MEASURED, NOT FIXED (P1.4's input)

This host, `win32`, Python 3.10.11, four-terminal tree. Two cold runs each:

| probe | measured |
|---|---|
| `import litellm` (cold) | **17.43 s**, then 25.96 s / 21.84 s on repeats |
| `import runtime.model_router` | **0.26 s** — and `litellm` NOT in `sys.modules` |
| `import provider_gateway + resilience + budget_governor + scheduler + worker` | **0.38 s**, `litellm` NOT loaded |
| `import runtime.model_capabilities` | **0.10 s**, `litellm` NOT loaded |
| `model_capabilities._from_litellm("gpt-4o-mini")` | **19.73 s** → `(128000, "litellm:max_input_tokens")` |
| same, for a `SYNTHETIC_PROVIDERS` provider | **0.00 s**, refused BEFORE the import |
| `litellm.completion` bind after import | **0.0000 s** |
| second `import litellm` in the same process | **0.00000 s** |
| `model_router._litellm_completion()` (the streaming seam) | **35.74 s** |

**The headline for P1.4 is a CORRECTION to the premise.** The brief says the
13-second import "lands on the first model call" and that the import sits in
`model_router.py`. **It is already lazy everywhere.** `model_router` does NOT
trigger it — 0.26 s, and `litellm` is absent from `sys.modules` afterwards.
Every `import litellm` in `runtime/**` is already inside a function:
`model_capabilities._from_litellm` (:1086), `model_router._completion_with_retry`
(:1000), `model_router._litellm_completion` (:1037). **The first trigger on a
real call is the CONTEXT-WINDOW rung, not the dial** — 19.73 s, and the
synthetic-provider guard at :1083 already short-circuits it. The number P1.4
should attack is therefore the ladder rung, and the 13–35 s is *one* cold cost
per process either way. Note the tree has measured 15.36 s (`AGENTS-05`),
32–34 s (ceiling-06) and 17–26 s (this round): **a 2× spread on the same
host**, so quote a range and never a single number.

Trigger sites in `runtime/`: `model_capabilities.py:1086`,
`model_router.py:1000`, `model_router.py:1037`, `provider_gateway.py:920`
(calls `model_router._litellm_completion()`).
Outside `runtime/`: `cli/auth.py:1340`, `cli/doctor.py:309`, `cli/onboard.py:265`.

### 6. Cross-terminal requests (NOT applied — `tests/`, `cli/`, `shared/` are other owners')

1. **T5 (`tests/test_scheduler_integration.py:348`) — one stale pin, one line.**
   `test_worker_exception_output_redacts_credentials` asserts
   `"[REDACTED]" in rendered`. `_safe_exception_text` now delegates to
   `shared.security`, whose placeholder is `[REDACTED_SECRET]`. **The security
   property still holds** — `assert sentinel not in rendered` passes; only the
   spelling differs. Change the second assertion to
   `"[REDACTED_SECRET] in rendered`. Please ALSO add: an ANSI-split key
   (`"sk-abc\x1b[0mdef123456789"` → currently `"key [REDACTED_SECRET]"`), a
   URL-userinfo key, a query-string key, and a monkeypatched-raising redactor
   → `(withheld: ...)`. Those four are the behaviours this round ADDED and none
   is currently pinned anywhere.
2. **T5 — pin `runtime/invariants.py`.** The 8 checks are a module, not tests;
   `tests/` owns the suite. `python -m runtime.invariants --json` returns
   `{checked, held, invariants[]}`, exit 2 on violation. Please assert
   `held == checked == 9` and add an inverted pin so the day someone fixes one
   of the recorded debt items it fails.
3. **T4 (`cli/runview.py::cost_reconciliation`) — the `unreceipted_calls`
   contract.** Checked structurally (§4) because the file is T4's. The load-
   bearing requirement: an **absent** ledger must report `ledger_available:
   False` WITH a reason and **can never** report `reconciled: True`; and
   "ledger not present" must stay distinguishable from "ledger present and
   empty". Today `ledger_available` and the reason are already there and
   correct — what is missing is the `reconciled` guard, which is currently
   `available and ...` so it happens to hold. Pin it.
4. **T5/T1 — `fsutil._redact_node` emits `[REDACTED]`, `shared.security` emits
   `[REDACTED_SECRET]`.** Two spellings of one answer already exist in the
   tree (`tests/test_ensemble.py:300`, `test_scheduler_integration.py:255`
   pin the first; this round's `:348` pins the second). Unifying them is one
   decision in `shared/security.py` and a pin sweep, and it is **T5's to call** —
   but until it is made, every assertion on the placeholder is a guess about
   which redactor ran.
5. **T2/T1 — no request.** `fsutil.atomic_write_json`'s Windows
   sharing-violation retry is pre-existing and correct.

### 7. Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)

- `python -m runtime.invariants` → **9/9 hold, exit 0**.
- Mutation probe → **13/13 detected**.
- `tests/test_model_router.py` + `test_ceiling_r2_13_capability_routing.py` +
  `test_ceiling_r2_14_budget_quota.py` → **151 passed**.
- `tests/test_ceiling14_resilience.py` + `test_prompt_cache_cost.py` +
  `test_provider_smoke.py` → **100 passed, 5 skipped** (the 5 are the
  no-credential provider smokes — **not passes**, and not claimed).
- `tests/test_scheduler_integration.py` + `test_ceiling06_orchestration.py` +
  `test_tracing.py` + `test_config_trace_state.py` → **128 passed, 1 failed**.
  The one red is §6.1, deliberately caused by this round and filed.
  `tests/test_ceiling_r2_14` + `test_ceiling14` + `test_ceiling_r2_13` → **188
  passed**, re-run AFTER both §3 fixes to prove neither tightened check
  regressed the documented budget bound.
- `python -m evals.run --check` → **14/14 CLEAN, exit 0**. No prompt changed,
  so this is a no-regression receipt and **not** a claim about model quality.
- `python -m ruff check runtime` on the 8 files this round owns →
  **All checks passed.** Whole-package: **6 findings, all pre-existing** in
  `abuse.py` (3) and `multirepo_tasks.py` (3), both files this round did not
  change. The two `F401`s in `model_router.py` that every earlier section of
  this file records as pre-existing (`typing.List` / `typing.Tuple`) are now
  gone — `ruff --fix` removed them and only builtin `list`/`tuple` are used in
  that file's annotations, so the removal is correct.
- `python -m compileall -q runtime` → **exit 0**.
- `git diff --check` scoped to the edited files → **exit 0**. Whole tree → 32
  findings, **all pre-existing markdown** (`cli/AGENTS.md`, `harness/AGENTS.md`,
  `runtime/AGENTS.md`); zero in Python source.

**Two things observed and NOT counted as passes.** (a) One
`PermissionError [WinError 5]` on `os.replace` in
`test_config_trace_state.py::test_task_state_resume_hydrates_from_disk` inside
a 4-file run; it **passed 3/3 standalone and did not reproduce** in the re-run
of the same 4 files (128 passed). `runtime/fsutil.py` was last modified
2026-09-24 and was **not** touched — this is the Round-7 sharing-violation
class under concurrent load. (b) `python -m ruff check runtime --fix` was run
across the whole package and **stripped two deliberate `# noqa` directives
from `runtime/abuse.py`** (`:226` `# noqa: N802`, `:695` `# noqa: BLE001`).
Both were **restored immediately** and verified restored by
`git diff -- <file> | grep noqa` returning empty. **Lesson: scope `--fix` to
the files you own in this tree.** No formatter was run over any shared file.

### 8. Not implemented, stated plainly

- **The litellm import was NOT touched.** Measured and handed to P1.4 (§5),
  as the brief requires.
- **`orchestration.py::_persist` was not verified** for the ~10 `state.error`
  assignments. `_event` redacts at `:2389`; whether `orchestration.json`
  re-writes them raw was NOT confirmed. The module is a large shared dirty
  file and this round did not open it.
- **`attempt_token` is still persisted unredacted** to `checkpoint.json`,
  `heartbeat.json` and the run-level `events.jsonl` (3 sites). It is a
  per-attempt bearer token, not a provider credential, so it is out of this
  round's threat model — but the trace copy is masked by `shared.security`'s
  `token` key rule while the journal is not, so **the trace under-reports the
  exposure the journal carries.** Recording it here rather than "fixing" a
  supervisor-auth design inside a security round.
- **`fsutil.atomic_write_json` and `append_jsonl` still redact nothing.**
  Every safe sink above is safe because its CALLER remembered. This round
  fixed the callers; a caller added tomorrow is unprotected.
- **`redact_payload` exists but has no caller** in `runtime/` yet — it is the
  seam for the structured sinks above once T5/T1 decide the boundary.
- **A truncated secret in `secrets=` leaks the tail.** `secrets=["sk-abc"]`
  against a 20-char key leaves `def1234567890`. That is a caller error (a
  prefix is not the secret) and shape-matching is `shared.security`'s job, so
  it is documented, not papered over.
- **No live-provider lane and no Docker lane were run.** No credential was
  inspected, printed, or retained. Every provider-shaped failure here is a
  constructed exception object. The §3(a) reproduction uses a **fictional**
  `tokenrouter`/`z-ai` string, not a live endpoint.

---

## P0/W2 T3 — the invariants are now FAILING-IF-BROKEN, not just reported (2026-10-01)

**NEW, four files, all inside `runtime/`: `runtime/test_honesty_pins.py` (35
tests), `runtime/test_boundary_signature_pins.py` (15),
`runtime/test_structural_predictor_guard.py` (16),
`runtime/test_supervision_and_quota_pins.py` (24) — 90 tests, 1 336 lines.
NOTHING else was edited.** No production file was touched in the end: the
three files that were temporarily regressed (§4) were restored **byte-for-byte**
and proved so by SHA-256, because all three are UNTRACKED since HEAD and
`git diff` cannot see them.

### 0. Read this first: these modules are NOT collected by a bare pytest

`pyproject.toml` pins `testpaths = ["tests"]`, so `python -m pytest` alone
collects **none** of this. Run them explicitly:

```
python -m pytest runtime/test_honesty_pins.py runtime/test_boundary_signature_pins.py \
                    runtime/test_structural_predictor_guard.py runtime/test_supervision_and_quota_pins.py -q
```

That is also why T5 owns a mirror (§3).

### 1. What each module is FOR, and the discipline every test follows

`runtime/invariants.py` (§P0/W1 T3 above) reports 9/9. A report nobody can
fail is a comment with a `pass` at the end. These four modules assert the
**live behaviour** of the modules that actually serve a run, and every one of
them is paired with a **CONTROL arm**, because the cheapest way to satisfy
"never does X" is to never do anything:

| module | invariant(s) | the anti-vacuity rule it leans on |
|---|---|---|
| `test_honesty_pins.py` | 1–8 + the docstrings + the `DEFAULTS` rules | "unpriced reports 0.0" is asserted only AFTER proving a bare number sort ranks the unpriced model CHEAPEST, so the gate is the thing under test and not a coincidence |
| `test_boundary_signature_pins.py` | W2.2 signature discipline | `**kwargs`-only doubles are refused; the real `call_model` and `execution.verify.verify` are asserted to NAME every keyword the gates send, so production is provably unaffected |
| `test_structural_predictor_guard.py` | the unshipped challenger | the default arm and the explicit arm must resolve to DIFFERENT estimators, or "unshipped" and "shipped" would be the same observable |
| `test_supervision_and_quota_pins.py` | W2.4 exemptions + quota/budget | quota and rate-limit are two arms of the same shapes; the two exception types must stay unrelated classes |

Three additional rules are asserted because they are the ones that keep
rotating out: **`sent` must agree with `status` on every row of an 8×5 sweep**,
**every one of the seven `EFFORT_STATUSES` must be REACHABLE** (a status
nothing can produce is untested vocabulary — `disabled` needs
`map_effort(..., parameter="none")` and `synthetic` needs
`synthetic_effort_plan`, neither of which the level sweep finds), and
**the `DEFAULTS` scans** assert that no `capability_*` / `difficulty_features`
/ budget key is ever published in `harness.config.DEFAULTS`, because a value
there is merged into every task and every eval arm.

### 2. The requirement that was stated WRONG, and what was pinned instead

The brief asked to prove that "calling `predict_structural` directly requires an
explicit argument". **`predict_structural(context=None, *, bands=None)` does
NOT** — it is callable with no arguments and returns a real answer. Asserting
that would have been a test that passes only while the signature is wrong.

What is pinned is the property that actually matters, and it is stronger than
the sentence asked for: **no CONFIGURATION path reaches the challenger.** No
`difficulty_features` value in `harness.config.DEFAULTS`, no environment
variable, and — by AST — every non-docstring `"structural"` literal in
`runtime/model_router.py` lives inside `_maybe_predict_difficulty` and nowhere
else (measured: 3 literals, all in that one function). The pinned control is
that the challenger is STILL reachable explicitly, because a guard that made it
unreachable would protect nothing.

The shipping gate itself is pinned BOTH ways, so the guard cannot rot into a
permanent refusal: a challenger win on a below-floor holdout (2 hard labels)
reports `ship=False` + `promising-but-unproven`, and **the same challenger on
an adequate holdout (3 hard labels) reports `ship=True`**. `MIN_HOLDOUT_HARD_LABELS
== 3` is asserted as a literal with exactly one assignment line in the module,
because it is a floor for honesty and no measurement may move it.

### 3. Cross-terminal requests (NOT applied — `tests/` is T5's)

1. **T5 — mirror these four modules into `tests/`, or add a `tests/test_runtime_pins.py`
   that imports them.** Right now `python -m pytest` collects ZERO of these 90
   tests (§0). The eight honesty tables, the boundary gates and the Phase-6 pin
   all need to run in CI.
2. **T5 — the Phase 6 inverted pin, exact name:**
   `test_phase6_revive_the_structural_predictor_is_a_deliberate_decision`.
   It must be **INVERTED** — it FAILS the moment
   `runtime/difficulty_structural_calibration.json` appears — and flipped in the
   same change that ships the challenger, after
   `python -m evals.difficulty_holdout` re-runs on an ADEQUATE hard-label count.
   The full intent text is `PHASE6_PIN_INTENT` in
   `runtime/test_structural_predictor_guard.py`; this round asserts only that
   the name is recorded and that the file is still absent today.
3. **T5 — `tests/test_module_reachability.py` was ALREADY RED, and this round
   adds four names to it.** Measured with this round's four files moved OUT of
   `runtime/`: `test_every_production_module_has_a_production_importer` reports
   **17** unrecorded orphans; with them present it reports **21** — the four new
   ones are `runtime.test_honesty_pins`,
   `runtime.test_boundary_signature_pins`,
   `runtime.test_structural_predictor_guard`,
   `runtime.test_supervision_and_quota_pins`. The scanner treats any
   `test_*.py` inside a production package as a PRODUCTION module, and 13 such
   modules already exist in `cli/`, `execution/` and `harness/`. The second
   failure, `test_no_recorded_exemption_may_be_stale`, names
   `extensions.skill_policy` and is **not this round's at all**. The clean fix
   is for the scanner to exclude in-package `test_*.py`, not four more
   `RECORDED_UNREACHED` lines.
4. **T4 — `cli/runview.py::cost_reconciliation` is now pinned BEHAVIOURALLY** by
   `test_honesty_pins.py` rather than structurally, read-only, from a runtime
   test: an absent ledger must report `ledger_available=False` WITH a reason and
   never `reconciled=True`, and "absent" must stay distinguishable from
   "present and empty". **This round found the missing `reconciled` guard is
   load-bearing by coincidence** — the expression is `available and ...`, so it
   happens to hold. It should be stated, not inferred.
5. **T1 — `harness/model_client.py::_boundary_names` /
   `_boundary_streams` and `harness/agent_loop.py::_verify_boundary_names` are
   now pinned from `runtime/`** (behavioural, read-only). The gate pins assert
   that `runtime.model_router.call_model` still NAMES `stream`, `on_delta` and
   `effort`, and that `execution.verify.verify` still names `rung_config`,
   `run_dir` and `phase`. If a signature is renamed, this round's tests fail
   first and say which production path went dark.

### 4. THE FINDING: `runtime/invariants.check_cache_unreported` has a real gap

Regressing `receipt_from_usage` so that an unreported provider becomes `miss`:

* `python -m runtime.invariants` → **9/9 HOLDS, exit 0** — the check does not
  see it;
* `runtime/test_honesty_pins.py` → **2 failed** (`..._is_unreported_not_a_miss`,
  `..._keeps_unreported_out_of_the_hit_rate_denominator`), the second failing
  with `cache_decided_calls` 1 instead of 0.

The W1 record says 13/13 mutations were detected, including "`unreported`→`miss`".
That is true of a monkeypatched `cache_tokens_from_usage`, and **false of the
LADDER in `receipt_from_usage`** — the check's fixture drives the token reader,
so editing the branch that chooses the status is invisible to it. The mutation
probe measured the seams it patched, not the seams the code has. **Reported,
not silently patched**: `runtime/invariants.py` is W1's file and this round
edited nothing, but T5/W1 should know the check is less sensitive than the
record claims.

### 5. FIVE regressions, each failed → restored → passed (transcripts in §8)

| # | live behaviour regressed | pins that fired |
|---|---|---|
| 1 | `BudgetGovernor.spent_usd` summed instead of taking the MAXIMUM | 3 (`..._sum_exceeds_the_cap`, `..._its_own_charge...`, the 9/9 receipt); `runtime.invariants` 8/9, exit 2 |
| 2 | `EffortPlan.parameters` dropped its `self.sent` guard | 3 (`..._but_sent_produces_no_parameters`, `..._never_clamped`, the 9/9 receipt); `runtime.invariants` 7/9, exit 2 — with `{'reasoning_effort': None}` on a request, the exact lie |
| 3 | `receipt_from_usage` mapped `unreported` → `miss` | 2; **`runtime.invariants` stayed 9/9** (§4) |
| 4 | `backoff_seconds` cap removed, then `begin_backoff` decoupled from it | 1 (`..._capped_and_monotone`) then 2 (`..._derived_from_the_same_backoff_value`, `..._two_different_numbers`, `240.0 != 120.0`) |
| 5 | the quota-first branch deleted from `classify_provider_failure` | **6** — every quota pin, and the failure mode is the recorded one: `kind='unknown_failure'`, `billing_url=None`, and `governed_completion` no longer raises `QuotaExhausted` |

Regression 5 is the load-bearing one for W2.4: with the branch gone, the
governed loop **retries an empty account** — the exact behaviour this module
was written to eliminate.

### 6. What is NOT implemented, stated plainly

- **`runtime/streaming.py:503` is the ONE site in `runtime/` that forwards a
  keyword to an injected boundary with no signature read** (`completion(**stream_kwargs)`
  with `stream=True` forced in). It is recorded in `FORWARDING_SITES` as kind
  `contract`, not `signature`, and the reason is a judgment worth recording
  rather than a bug: `stream=True` is not an OPTIONAL capability keyword, it is
  the request's SHAPE. Degrading it would be a different lie — the caller would
  believe it streamed while the assembler collected nothing — and
  `tests/test_streaming_live.py:351` deliberately requires
  `attempts[0]["stream"] is True`. Both directions are now pinned (this module
  and that one), so the decision is visible instead of implicit. **If someone
  decides it should degrade, `FORWARDING_SITES` is where the change goes.**
- **No config/env/CLI selector for the structural predictor was ADDED.** The
  guards prove none exists; adding one is Phase 6's decision, not this round's.
- **No in-package `conftest.py`.** These four modules need none (no fixtures, no
  Docker, no network), but that also means they inherit whatever the root
  `conftest` does — currently nothing that affects them.
- **No Docker lane and no live-provider lane were run**, and neither is claimed.
  Every provider-shaped failure in `test_supervision_and_quota_pins.py` is a
  constructed exception object; no credential was inspected, requested or
  retained.
- **`git diff` cannot verify the three temporarily-regressed files**, because
  `budget_governor.py`, `model_capabilities.py` and `prompt_cache.py` are
  UNTRACKED since HEAD. SHA-256 was used instead and all three match the
  pre-regression hashes exactly (§7).

### 7. Verification actually run (this tree, `PYTHONIOENCODING=utf-8`, `-p no:randomly`)

1. `python -m runtime.invariants` → **9/9 invariants hold, exit 0**.
2. The four pin modules → **90 passed in 1.53s**.
3. `tests/test_model_router.py tests/test_ceiling_r2_13_capability_routing.py
   tests/test_ceiling_r2_14_budget_quota.py` → **151 passed in 14.65s**.
4. `tests/test_ceiling14_resilience.py tests/test_prompt_cache_cost.py` →
   **100 passed in 94.91s**.
5. `tests/test_agt_08_effort.py tests/test_streaming_live.py
   tests/test_module_reachability.py` → **131 passed, 2 failed**. **Both
   failures are in `tests/test_module_reachability.py` and are NOT this round's
   defects** — measured with this round's four files moved out of `runtime/`,
   the orphan count is **17** without them and **21** with them, and the other
   17 belong to `cli/`, `execution/` and `harness/` (§3.3). No assertion was
   weakened and no `RECORDED_UNREACHED` line was added.
6. `python -m evals.run --check` → **14/14 CLEAN, exit 0**. No prompt changed,
   so this is a no-regression receipt and NOT a claim about model quality.
7. `python -m ruff check` on the four new files → **All checks passed**;
   `python -m compileall -q runtime` → **exit 0**; whole-package
   `python -m ruff check runtime` → **6 findings, all pre-existing**, 3 in
   `abuse.py` and 3 in `multirepo_tasks.py`, the same split W1 recorded, in
   files this round did not touch. **`ruff --fix` was SCOPED to the four new
   files** (7 findings it fixed were all mine); the pre-existing `noqa`
   directives in `abuse.py` were not touched, unlike the W1 accident.
   `git diff --check -- runtime` → 3 findings, all pre-existing trailing
   whitespace in `runtime/AGENTS.md` markdown, none in a file this round wrote.

## AGT-05 - the planner role derives its tool list from the plan-mode gate (2026-09-29)

**Files owned/edited:** `runtime/roles.py` only. `runtime/scheduler.py`,
`runtime/worker.py`, `runtime/orchestration.py`, `runtime/subagents.py` and
`runtime/model_router.py` were NOT edited. No Boundary signature, event kind,
or completion status changed.

### The change, and why it closes a hazard rather than adding a feature

The `planner` profile's `visible_tools` was a HAND-WRITTEN list of eleven
names. It is now derived from
`harness.agent_kernel.subagents.plan_mode_tools()` - the same capability gate
the kernel's plan-phase research subagent runs under - via
`_planner_tools()`. Three reasons, in order:

1. **A hand list drifts in both directions.** A read tool added to
   `harness.tools` was invisible to a planner, and a renamed tool broke it.
   The derivation has neither failure mode: adding a read widens both the role
   and the subagent; adding a mutating tool widens neither.
2. **A planning role that could mutate is a plan phase that can act.** The
   profile is the orchestration-facing form of "plan mode", and the whole point
   of the gate is that it is a CAPABILITY gate rather than an instruction. A
   second list is a second answer to "what may a planner not do".
3. **`task` stays out.** `task` is a `control`-class tool and therefore inside
   the granted capability, so it is withheld by name
   (`PLAN_RESEARCH_WITHHELD_TOOLS`). A planner that could fork work has an
   unbounded budget by construction, and the ceiling-06 "no profile exposes a
   spawn tool by default" invariant must not be broken by a derivation.

### What an operator or another module should read

- `planner_capability_gate()` - the planner role's effective surface and every
  withheld capability WITH its reason and the tools it carried. Same receipt the
  research subagent reports; exposed here so a caller can ask the role-layer
  question without reconstructing the catalog by hand.
- `planner_withheld_reason(capability)` - one refusal's text.
- The gate follows CAPABILITIES, not effect classes. `ask` and `finish` are
  `control`-class and ARE reachable (a planner that cannot end its turn cannot
  return a plan). A test asserting "non-read_only implies unreachable" encodes
  the wrong rule - see `tests/test_agt_05_plan_isolation.py`.

### The deliberate narrowing: the planner role loses `memory`

The old hand list included `memory`. The gate withholds the `memory`
capability, so the role no longer has it. That is correct for a plan phase:
`memory_record` is a WRITE, and `harness.knowledge` (which owns
`memory_record_enabled` and the authorization) is a different surface from the
read-only memory query. The role WIDENS on reads (it gains `list`, `image`,
the four `git_*` reads, `read_symbol`, `find_definition`, `find_references`
and `blast_radius`) and NARROWS by one: the memory query. Every addition is
`read_only` in the canonical catalog.

### Verification

`tests/test_orchestration.py` -> **19 passed** (unmodified).
`tests/test_agt_05_plan_isolation.py` -> **47 passed**, including
`test_the_planner_role_shares_the_researchers_gate` (the role's tool set IS
the gate's, and no mutating/shell/network/mcp/task tool is reachable) and
`test_the_gate_is_derived_from_the_catalog_not_a_hand_list` (the gate equals
the capability surface, exhaustively). No live-provider or Docker lane was run
and neither is claimed.

**One red result observed, measured, and NOT counted as a pass in the run where
it failed:** `TestOrchestratorSubprocess::test_child_wall_timeout_retries_and_
resumes` failed once inside a combined run (`status != "success"`, the
`+ timeout` diff at `tests/test_orchestration.py:454`) and PASSED standalone and
in two consecutive whole-file runs with this round's change in place. It is the
host-load class already documented in this file, and this round MEASURED the
margin rather than assuming it:

- the child imports `litellm` in **15.36s** on this host, against the test's
  `max_workflow_wallclock_s=15.0` - so one cold child import can consume the
  entire workflow budget, and the second attempt has nothing left;
- AGT-05's planner list costs **0.841 ms** per registry+policy construction
  against **0.655 ms** for the old hand list - **+0.19 ms against a 15,000 ms
  budget** (0.0013%).

A controlled A/B (planner reverted to the old hand list -> 19 passed;
restored -> 19 passed twice) plus the measured 0.19 ms cannot account for a
15-second miss. **Not attributed to this round, no assertion was weakened, and
it is not counted as a pass in the run where it failed.**

## AGT-08 — the effort ladder, honestly reported, and the cheap summariser tier (2026-09-28)

**Files owned/edited:** `runtime/model_capabilities.py`, `runtime/model_router.py`,
`runtime/provider_gateway.py`, `runtime/checkpoint.py`, `harness/model_client.py`,
`harness/config.py`, `harness/agent_kernel/strategy.py` (compaction tier only),
`harness/agent_kernel/checkpoints.py`, `shared/agent_contracts.py` (one additive
field), `cli/commands.py` (the `/effort` registry row + pure helpers), and the
three minimal dispatch call sites in `cli/interactive.py` / `cli/tui.py`. NEW
`tests/test_agt_08_effort.py`. **The verifier gate, every completion status, and
every Boundary 0-5 signature are unchanged**; the suite pins that the success
mint reads no effort setting at any level.

### 1. The rule the whole round exists to enforce

**A level that is not sent is reported as not sent.** `EffortPlan` is the only
thing a caller merges, and its `parameters` property is empty for every status
except `sent` — so `unsupported_model` is *structurally* unable to become
"silently ignored". The closed vocabulary:

| status | meaning |
|---|---|
| `sent` | a real provider parameter is on the request |
| `auto` | no level requested; the provider's own default is used |
| `unsupported_model` | the model family has no declared effort knob |
| `unsupported_level` | the family has a knob, but not for this level |
| `disabled` | the caller pinned `effort_parameter` off, or named an undeclared one |
| `synthetic` | a harness-served provider that ignores request parameters |
| `invalid` | the value is not a level, and is echoed back |

**A level the family does not accept is never clamped.** `/effort max` on a
three-level family returns `unsupported_level` and sends nothing; it does not
quietly become `high`. That is the whole difference between a ladder and a
slider that lies.

### 2. The knobs are real parameters, and the MODEL picks the family

`openai → reasoning_effort` (low/medium/high), `anthropic → thinking`
(`{"type": "enabled", "budget_tokens": N}`, all five levels), `google →
thinking_budget`. The **model name** decides, not the provider name: an
`openai`-compatible gateway fronts Claude and Gemini too, and attaching
`reasoning_effort` to a model that rejects it turns a cost knob into a 400. So
`gpt-4o` deliberately claims nothing while `gpt-5` claims `openai` — the
`_MODEL_FAMILY_HINTS` table is narrow on purpose. An operator can add a family
(`register_effort_knob`), and a malformed declaration RAISES rather than
defaulting, matching `register_capability`.

### 3. The defect the eval matrix caught, and the rule it produced

The first version forwarded `effort` to the boundary whenever it accepted
`**kwargs`. The daily-driver suite went red with

```
planner failed: ScriptedModel.__call__() got an unexpected keyword argument 'effort'
```

A permissive signature is NOT evidence of support — it accepts a keyword and
then hands it to something that rejects it, which is a run-killing
`TypeError`, not a degraded feature. `ModelClient._boundary_names(fn, "effort")`
now forwards only when the boundary **names** the parameter, which is the same
discipline `_boundary_streams` already uses for `stream`. Both the failure and
the rule are pinned
(`test_a_permissive_boundary_is_not_evidence_of_support`).

### 4. Every receipt says what was asked for AND what reached the provider

The ledger row, `get_last_usage()` and the unified `model_routed` event all
carry `effort`, `effort_status`, `effort_sent`, `effort_parameter`,
`effort_value`, `effort_model`, `effort_detail` — **including on a FAILED
attempt**, because a cost claim about a failed call needs the same explanation.
`ModelClient`'s per-call record carries `effort` plus the router's
`effort_*`, and `cli/main.py::_result_json` publishes them under one additive
`effort` key (`_effort_json`). The historical document carried only
`len(model_calls)`, which made "ran at high" and "asked for high and the
provider ignored it" the same bytes to a script; the new key carries the
level, the per-call statuses and parameters, a `supported` boolean, and up to
`EFFORT_RECEIPT_LIMIT` (50) bounded per-call receipts plus a
`receipts_truncated` count.

The resilient pipeline resolves the plan **per candidate** and memoises it, so
a refusal row and the dialed row for one target can never disagree, and a
fallback onto a model with no knob is reported rather than silently ignored.

### 5. Resume identity — a high-effort run is not a low-effort run

`effort_identity` joins `_IDENTITY_FIELDS` on **both** checkpoint paths
(`runtime.checkpoint` and `harness.agent_kernel.checkpoints`) plus the additive
`shared.agent_contracts.Checkpoint.effort_identity`. Resolution goes through the
same authority, so `hi` and `high` are one identity. A mismatch takes the
existing fail-closed path (a fresh attempt, journalled) — never a quiet
continuation at a different level. `harness.agent_kernel.checkpoints.with_effort`
is the one seam that carries the rung from the run's config into a `RunSpec`'s
metadata, so all three strategies agree.

Note the identity is **always non-empty** (`"auto"` at minimum), which is what
makes `checkpoint_identity_matches`' existing "all identity fields must be
non-empty" rule also demand an effort field. A checkpoint that never recorded a
rung therefore does NOT authorize a resume.

### 6. Compaction defaults to the cheap tier

`context_compaction_tier` (`cheap` | `expensive` | `run`, default `cheap`).
Summarisation is the most mechanical job in the loop, and it was running on
whatever model the run used. `_summary_gateway` now prefers the run's
`model_tiers["easy"]` model, falling back to `runtime.config.DEFAULT_MODEL_TIERS["easy"]`.
`context_compaction_model` still wins outright. The `context_compacted` receipt,
the `compactions.jsonl` row and the journal event all gain `compaction_tier`,
`compaction_tier_configured`, `compaction_tier_model` and
`compaction_tier_honoured`; the pre-existing `compaction_model*` fields are
unchanged in meaning, so R2-08's receipt pins still pass untouched.

Two decisions worth keeping: a cheap tier that IS the run's own model returns
the run's own gateway (byte-identical to the pre-round path, no second
gateway, no second model), and an unrecognised tier falls back to `cheap`
rather than to the expensive run model — a typo must not make summarisation
cost frontier prices.

### 7. `/effort` is a real command, and the composer teaches it

One `CommandSpec` row (alias `/thinking`, `in_flight_policy="allow"`) plus the
public pure helpers `EFFORT_LADDER`, `effort_levels`, `effort_receipt`,
`apply_effort`, `render_effort` in `cli/commands.py`. The composer hint, the
palette, the headless policy table and `/help` are all projections of the
registry, so `/eff` teaches the whole ladder while it is being typed — nothing
about this feature is buried.

`apply_effort` writes BOTH `state["effort"]` and `NEO_EFFORT`. The second write
is not belt-and-braces: the router resolves the environment when a run's config
is assembled by a path this command does not own (a worker subprocess, a mode
module, the headless runner), and writing only the session key would leave
exactly the "set to high that silently does nothing" failure the ladder exists
to remove. `render_effort` returns plain lines and the two surfaces wrap them
in `escape()`; the test pins that the first line can never open a bracket.

### 8. Config keys

| key | default | why |
|---|---|---|
| `effort` | `"auto"` | behaviour-**NEUTRAL** (no parameter is sent), which is the only reason it is safe in a table merged into every task and every eval arm. A test pins the auto request kwargs are byte-identical. |
| `effort_parameter` | `None` | `None` = "use the family's own knob" (NOT "off" — the router passes `None` on every unconfigured run, so reading it as off would silently disable the feature). A string names the parameter; `none`/`off`/`false`/`0`/`no` send nothing. |
| `context_compaction_tier` | `"cheap"` | behaviour-CHANGING, and it is the fix the brief asked for. |
| `NEO_EFFORT` (env) | — | read by `harness.config.get_config` (the ONE env-aware merge) and by `resolve_effort` for callers that pass a raw `Task.config` to the router. |

`get_config` checks the env against the CALLER's dict, not the merged one,
because `DEFAULTS["effort"] = "auto"` is always present after the merge and
would otherwise make the environment unreachable.

### 9. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_08_effort.py` → 67 passed** (host-only: no Docker, no
  provider, no network; every provider-shaped assertion uses a fake litellm
  installed through the router's own seam). The six required proofs are present
  and named: `test_every_level_maps_to_a_real_parameter_or_says_why_not`,
  `test_the_operator_can_name_the_parameter_explicitly`,
  `test_a_success_row_records_the_level_the_parameter_and_the_model`,
  `test_the_runtime_checkpoint_identity_includes_the_effort` /
  `test_the_strict_identity_requires_a_non_empty_effort`,
  `test_every_model_call_record_carries_the_effort`, and the gate pair
  `test_no_completion_path_reads_the_effort_setting` +
  `test_the_mint_condition_is_still_the_verifier_triple`. Compaction is proven
  on the REAL kernel (the fixture reads a 150 KB file repeatedly until it
  compacts, then the `context_compacted` receipt and `compactions.jsonl` row
  are read back off disk).
- `test_model_router.py` + `test_config_trace_state.py` +
  `test_cli_command_system.py` → **137 passed**.
- `test_ceiling_r2_08_backoff_context.py` → **25 passed** (the compaction
  receipt pins, unchanged). `test_context_budget_engine.py` → **18 passed**.
- `test_agent_kernel.py` + `test_agent_loop.py` → **109 passed**.
- `test_scheduler_integration.py` + `test_ceiling_r2_13_capability_routing.py` +
  `test_ceiling14_resilience.py` + `test_prompt_cache_cost.py` →
  **200 passed**.
- `test_cli_slash2.py` + `test_cli_terminal_parity.py` + `test_cli.py` →
  **89 passed**. `test_tool_protocol.py` + `test_workspace_security.py` +
  `test_steering.py` + `test_recovery_steering.py` → **191 passed**.
- `test_daily_driver_evals.py` + `test_evals_run.py` + `test_evals_tasks.py` +
  `test_modes.py` → **168 passed**.
- `python -m evals.run --check` → **14/14 CLEAN**, exit 0.
- `python -m evals.run --suite daily-driver --no-docker --json` → **52/52 case
  arms ok, 26/26 valid comparisons, `zero_false_verified_successes=true`,
  `zero_unauthorized_mutations=true`, `zero_lost_edits=true`, `fail_count 0`**
  — byte-identical to the recorded pre-round baseline. The FIRST run of that
  matrix was **26 arms red on `dd_05_skill_model_context`** and that is the
  `**kwargs` defect in §3; it is fixed and the case now passes standalone in
  both arms (`completed_verified`, `skill_model_content_receipt: true`).
  Overall readiness is reported **`NOT_READY`** and that is honest: the Docker
  lane, the live-provider lane and the sampled manual evidence were not
  selected.
- **One red in a combined run, attributed and NOT counted as a pass:**
  `tests/test_tui_contract.py::test_live_tui_populates_status_diff_and_non_vacuous_performance`
  failed in the 5-file `test_cli_runview/tracelog/polish/tui_contract/tui_layout`
  run and **passed 4/4 standalone** (10.5 s, 11.6 s, 9.3 s, 10.0 s). It is
  the host-load-sensitive probe this repo's own `cli/AGENTS.md` records in four
  separate rounds; no assertion was weakened.
- `ruff check` clean on every file this round created or edited, except the
  PRE-EXISTING findings recorded in `runtime/AGENTS.md`
  (`typing.List`/`typing.Tuple` unused in `model_router.py`) and two in-flight
  findings in other terminals' files (`kernel.py` `typing.Sequence`,
  `strategy.py` `.subagents.plan_mode_tools`), all left alone rather than
  "fixed" inside another module's file. `ruff format` applied to the new test
  file only. No formatter was run on the shared dirty files.
- **No Docker lane and no live-provider lane were run.** No credential was
  inspected, requested, or retained.

### 10. Not implemented, stated plainly

- **No `auto` inference.** `auto` means "send nothing, let the provider
  decide"; it does NOT mean "guess a level from the difficulty hint". Wiring
  the existing difficulty prediction to a level would be a second routing
  mechanism, and the brief asked for control, not for a new guess.
- **The knob table covers three families.** A bring-your-own model resolves to
  the honest `unsupported_model` row until an operator declares a knob. Nothing
  probes a live provider to discover its parameters (that would be a billable
  call, the same reason `context_window` takes a caller-supplied probe).
- **`effort` is not per-call in the legacy `harness/core.py` step session.**
  It reaches every call through `ModelClient` and the router context, so
  `/effort` and `Task.config` both work; there is no per-step override.
- **`/effort` is session-scoped, not persisted.** It writes the session key and
  the environment, not `settings.toml`. Persisting an effort level into a
  checked-in file is an operator decision, and the brief asked for config, env
  and the command — all three of which exist.
- **The TUI has no effort widget and no header cell**; the command renders into
  the transcript and re-renders the header. `cli/tui.py` and the composer hint
  bar are other owners' surfaces, and a `ContextPanel` row is filed below.
- **`_absorb_spend` is unchanged**, so a cheap summariser's spend is still
  folded into the run's own totals. That was already correct (R2-08) and this
  round did not need to touch it — the pinned test proves the receipt names the
  cheap tier on both the journal event and the durable `compactions.jsonl` row.

### 11. Cross-terminal requests

1. **`cli/runview.py` (`model_call_receipts`, `cost_reconciliation`) — surface
   the effort on `/cost`.** The router now writes `effort` / `effort_status` /
   `effort_parameter` on every ledger row, so `/cost` can print "high
   (reasoning_effort)" beside a price and the reconciliation can explain a cost
   delta by level. It needs no new data source, only a column. `cli/runview.py`
   was not this round's file.
2. **`cli/tui_components.py` (`ContextPanel`) — one `effort: high` cell.** The
   resolver (`cli.commands.effort_receipt`) is pure and already returns the
   current level plus the plan, so the panel needs one call and one row. Not
   added here because `tui_components.py` is the UXP-04 owner's file and this
   round is a model-layer change.
3. **`cli/main.py::_result_json` — a top-level `effort` beside `model_calls`.**
   The per-call records already carry it, so `--json` is explainable today; a
   single run-level field would be friendlier. `cli/main.py` is not this
   round's file and the R2-17 request about routing `_result_json` through
   `runview.effective_terminal_status` is still open there.
4. **`evals/routing_capability.py` — an effort arm is a natural sibling of the
   capability ablation.** It is deliberately NOT an arm in `evals/run.py`, for
   the same reason that driver is not: the prompt suite pins
   `use_mock_provider: True`, so a knob that is only sent to a real provider
   would compare two identical configurations and publish a vacuous "no
   delta". The mock lane reports `synthetic`, which is the honest answer for
   scripted responses and a vacuous arm.
5. **Nobody else needs to read `effort`.** It is a model-request parameter. If
   a completion path, a verifier, or a policy engine ever reads it, that is a
   defect, and `test_no_completion_path_reads_the_effort_setting` will say so
   before it ships.

# runtime/ — Terminal 3: Concurrency, Reliability, Model Routing

## VEX-CEILING-R2-14 — budget governor, quota awareness, 429 safety (2026-09-27)

**NEW `runtime/budget_governor.py` is the ONE authority for money, quota, and
the clock. Read this before touching `runtime/scheduler.py`,
`runtime/worker.py`, or the budget region of `harness/core.py`.**
`runtime/model_router.py` was NOT edited (R2-13 owns it) — the governor reaches
the dial through the router's existing Ceiling-14 delegation point.

Three coupled defects, one root cause (nothing above the provider dial knew
about money, quota, or its own clock). What changed:

| concern | one authority | where it is read |
|---|---|---|
| quota vs rate limit | `classify_provider_failure` | `governed_completion`, `provider_resilience.classify_retry` |
| backoff length | `backoff_seconds` | `governed_completion`, `begin_backoff`, the exemption |
| spend + cap | `BudgetGovernor` | the dial, the harness loop, `budget.json` |
| deadline | `BudgetGovernor.deadline_epoch` | the governor, `max_wallclock_s` |
| hang-kill exemption | `supervision_exemption` / `state_stale_exempt` | `scheduler._check_timeouts` |

### 1. The backoff and the watchdog now agree — ONE mechanism, two reasons

`supervision_exemption = {reason, until_epoch, seconds, grace_s, ...}` in the
runtime checkpoint **generalizes** the approval-park marker rather than adding a
second exemption. `state_stale_exempt(cp)` reads it; the historical
`awaiting_approval` boolean still works byte-for-byte (it is still written, and
`supervision_exemption()` falls back to it), so every existing approval test is
unchanged. A `finished` checkpoint is exempt exactly as before.

Three properties are load-bearing:

* **The window is DERIVED from the backoff**, from the same
  `backoff_seconds(attempt, ...)` the retry loop is about to sleep. A test
  asserts `window.until_epoch - window.started_epoch == backoff_seconds(n)`,
  so the measured 225 s backoff can no longer meet a 30 s kill window.
* **The WAIT and the LICENCE are two different numbers.** `seconds` is what
  the worker sleeps; `seconds + grace_s` is how long the kill is skipped.
  `sleep_in_backoff` uses `backoff_remaining_s`, never `remaining_s`. This was
  a real bug found by the end-to-end test: sleeping the licence added the grace
  to every wait and turned a 225 s backoff into a 255 s one.
* **`grace_s` IS the watchdog's own window.** The worker passes
  `hang_heartbeat_stale_s` (default `DEFAULT_HANG_STALE_S`, which now lives in
  `runtime/config.py` and is re-exported from `scheduler.py`) as the grace. A
  worker whose backoff just expired still has to land the retried call and write
  state; without the grace the state-stale kill fires the instant the exemption
  lapses — the same defect one moment later. One constant, two readers.

An **expired** exemption is not an exemption, so a worker that stops making
progress after its wait is killed exactly as before. The heartbeat kill and the
wall-clock cap are **never** exempted. Every skip is journaled as
`hang_exempt` with the reason and the measured `state_age_s`, so a reader can
see a worker was NOT killed and why.

**OFF arm:** `hang_backoff_exempt: False` in `Task.config` restores the
historical state-stale kill for a backing-off worker and journals
`hang_exempt_suppressed`. The end-to-end test uses it as the A/B that makes
"the worker survived" a measurement rather than a claim. The approval-gate
exemption is deliberately NOT switchable — an operator disabling the backoff
fix must not silently re-introduce the mid-gate kill.

### 2. Quota is NOT a rate limit

`runtime/provider_resilience.py::_RATE_LIMIT_MARKERS` used to CONTAIN
`"quota exceeded"` and `"insufficient_quota"`, so an empty account was told to
back off and retry — which cannot help and spends what is left. The quota
markers now live in their own set (`QUOTA_MARKERS`, re-exported from the
governor so there is one table) and `classify_retry` tests them **FIRST**,
returning `kind="quota_exhausted"`, `retryable=False`, `provider_fault=False`.

`provider_fault=False` is the important part: the endpoint is healthy and the
account is empty, so a circuit breaker must not be tripped and a failover must
not be attempted — a second target on the same billing account spends the same
absent money. In `provider_gateway.resilient_call_model` a `QuotaExhausted`
**re-raises immediately**: no retry, no backoff, no next candidate.

`QuotaExhausted` carries a `ProviderFailure` with a `billing_url`
(`billing_url_for(provider)`, a real page, never empty). The terminal outcome is
recorded in **five** places so it is not just a message:
`checkpoint.json`'s `terminal_reason` + `quota`, the run journal's
`finish.terminal_reason`, `logs/{task_id}/budget.json`'s `quota` block, the
`quota_exhausted` trace event, and the exception text.
**`TaskResult.status` deliberately stays `"error"`** — `shared/types.py` is
another owner's file and every consumer switches on that four-value vocabulary;
adding a value there is a cross-terminal request (§6), not this round's edit.

Status code is deliberately **not** the discriminator: several providers return
quota as a 429, and a test pins that both shapes classify differently.

### 3. Per-call budget — the granularity, stated honestly

`BudgetGovernor.authorize_call()` prices the NEXT call before it is dialed and
refuses one that cannot fit. Installed at **both** places that dial:
`provider_gateway` (before the request is built — a refusal raises
`BudgetRefused`, recorded on the ledger as `skipped_reason="budget_refused"`)
and `harness.core._bind_budget_guard` (wrapped around the run's
`ModelClient.call` instance, idempotent via `_neo_budget_guarded`).

**The enforced bound is `cap + the price of the final call`, not `cap`.** A
call's price is not knowable before it is made, so the reservation is an
estimate. Declare `max_completion_tokens` and the reserve becomes a sound
upper bound (`DEFAULT_UNOBSERVED_COMPLETION_TOKENS = 512` is the deliberately
small fallback; a large governance-chosen default would make the cap fire early
on cheap models, which is a different lie — a budget the user never spent).
This replaces the measured **9.28x** attempt-level bound with **+1 call**.

The pre-check's price comes from **R2-13's**
`model_capabilities.estimate_cost(...)`, so the cap and the cost report can
never disagree about a model. This module has **no price table of its own** and
never infers price state from a number: `BudgetVerdict.price_state` is R2-13's
`priced` / `free` / `unpriced` plus this module's own `declared_bound` /
`uncapped` / `cap_reached` / `quota_source`. An unpriced model with no declared
bound is REPORTED as `unpriced` — the check cannot refuse on price and the
receipt says why, which is the opposite of R2-13's defect.

Two further rules, both fail-closed:

* **A refusal is STICKY.** The cap is fixed at construction, so letting a
  later, smaller call squeeze through would let a run dribble past a cap it has
  already been told it reached.
* **`spent_usd()` is the MAXIMUM, never the sum**, of the governor's own
  charges and the bound `spend_source`. `ModelClient` never sees a provider
  fallback's earlier charges; the governor never sees the difficulty-classifier
  call. The max covers both without double-counting, so the cap can only fire
  **earlier** than either source alone — never later. A `spend_source` that
  raises is ignored, never allowed to disable the cap.

`harness.core.over_budget()` reads `governor.exhausted` when a governor is bound
and otherwise evaluates the historical expression **byte-identically**. An
unbound run emits a `budget_governor_absent` trace row, so attempt-level-only
enforcement is never indistinguishable from a cap that was never configured.

`budget_cap_usd` stays a `harness/config.py` DEFAULTS key (it is the harness's
to enforce). The worker resolves it through `harness.config.get_config` —
reading only `Task.config` would silently give the governor **no cap**
whenever the caller relied on the default. **No R2-14 key is in any DEFAULTS
table**; a test fails if one appears.

### 4. The live receipt — `logs/{task_id}/budget.json`

Next to `state.json` and `trace.jsonl`, written through the ONE atomic writer
(`write_budget_receipt` → `fsutil.atomic_write_json`) and published **at
construction**, so a run's budget is visible from the start rather than
appearing after the first charge. Carries `cap_usd` / `spent_usd` /
`reserved_usd` / `remaining_usd` / `exhausted`, `deadline_epoch` and
`clock` (both on `runtime.fsutil.now_epoch`), the backoff block
(`base_s`/`cap_s`/`grace_s`/`count`/`seconds_total`/live `window`), the `quota`
block, call counters, and a bounded `recent` verdict log. A `budget` unified
trace event rides each publish. An unwritable receipt is reported and never
raises — observability must not fail a run.

### 5. What is NOT done (stated, not implied)

- **`runtime/model_router.py` was not edited.** The pre-call check reaches the
  dial through `provider_gateway` (the router's existing Ceiling-14
  delegation point), so a task with **no** Ceiling-14 key in its router context
  never enters that pipeline and therefore gets no dial-boundary check — the
  harness-loop guard still covers it, but the dial-boundary one does not. A
  one-line addition to `_RESILIENCE_CONFIG_KEYS` (R2-13's file) would close
  that gap; it is a request, not an edit (§6).
- **`TaskResult.status` has no `quota_exhausted` value** (§2).
- **No CLI/TUI surface.** `budget.json` and the trace are the visibility; a
  live "budget $0.42 / $2.00" line needs `cli/runview.py`, which is not this
  module's file (§6).
- **The governor is per-process.** A 50-way fan-out gives each worker its own,
  so a shared cap across concurrent tasks does not exist. One cap per task is
  the documented scope.
- **`runtime/budget_governor.py` does not import `provider_resilience`** and
  vice versa. They agree because `provider_resilience` imports the marker sets
  FROM the governor and tests quota first; a test asserts the two classifiers
  reach the same action.
- **No live-provider lane was run.** Every provider-shaped failure in the suite
  is a constructed exception object.

### 6. Cross-terminal requests

1. **R2-13 (`runtime/model_router.py`) — one line, and it closes a real gap.**
   Add `"budget_governor"` to `_RESILIENCE_CONFIG_KEYS` so a task carrying a
   governor enters the resilient pipeline (and therefore the dial-boundary
   per-call check) even with no other Ceiling-14 key. The key-presence test
   means the worker's always-present governor is harmless. Nothing else in the
   router needs to change; this round deliberately did not touch the file.
2. **`shared/types.py` owner** — `TaskResult.status` is
   `Literal["success","failed","error","timeout"]`. Add
   `"quota_exhausted"` if the terminal status is to be switchable in-process;
   `runtime/serialize.py` and `scheduler._completed_result` would need the same
   value, and every CLI/eval consumer that switches on the status. Until then
   `status="error"` + `terminal_reason` is the contract, and it is
   machine-readable.
3. **`cli/runview.py` / `cli/tui.py` owner** — read
   `logs/{task_id}/budget.json` (or the `budget` trace event) to show remaining
   budget on the live card, and render `quota.kind` from the `quota_exhausted`
   event's `billing_url` as "your quota is exhausted — add credit at <url>"
   rather than a generic error. The file is written and stable.
4. **`harness/model_client.py` owner (optional, and this round did NOT do
   it)** — the per-call pre-check is installed by `harness/core.py` wrapping
   the instance. Moving it into `ModelClient.call` would cover every client
   (the kernel's `ModelGateway` included) instead of only `run_task`'s, at the
   cost of `harness.model_client` depending on `runtime.budget_governor`. The
   kernel's `ModelGateway` model path is currently **not** covered by the
   per-call check; it is covered by the attempt-level one.

### 7. Config keys (all optional; none in any `DEFAULTS`)

| key | read by | default | what it does |
|---|---|---|---|
| `budget_cap_usd` | governor (via `harness.config.get_config`) | `2.0` (pre-existing harness default) | the spend cap |
| `budget_reserve_per_call_usd` | governor | absent | declared per-call bound, used only when a model is `unpriced` |
| `hang_heartbeat_stale_s` | worker → governor grace **and** the scheduler's kill | `DEFAULT_HANG_STALE_S` (30.0) | one number, two readers |
| `hang_backoff_exempt` | scheduler | absent (= on) | explicit `False` restores the pre-round state-stale kill (OFF arm) |
| `rate_limit_retries` / `rate_limit_backoff_s` | governor + gateway | `4` / `15.0` (pre-existing runtime defaults) | the ONE backoff schedule |
| `max_completion_tokens` | governor reserve | absent | declaring it makes the reserve a sound upper bound |

### 8. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_ceiling_r2_14_budget_quota.py` -> 60 passed.** Host-only: no
  Docker, no provider, no network. Includes the four required proofs, each named
  after the behaviour:
  `test_a_worker_in_a_429_backoff_is_not_hang_killed` (real Scheduler, real
  worker subprocess, real `governed_completion`, real 429; backoff 4.0 s vs hang
  window 2.5 s; the window is measured to have outlasted the threshold, and the
  marker is asserted cleared with `finished`);
  `test_the_same_backoff_with_the_exemption_off_is_still_hang_killed` (the A/B);
  `test_a_quota_error_in_a_worker_ends_the_run_without_a_retry` (real worker,
  `status="error"`, no verification, no diff, `terminal_reason` on the
  checkpoint + the run journal, `backoff.count == 0`, no `crash_retry` — the
  task is not relaunched into the same empty account);
  `test_a_budget_of_x_never_exceeds_x_by_more_than_the_final_calls_price`
  (self-calibrating cap, each call charged 2.5x its reservation so the tail is
  real, sum read back from a JSONL ledger);
  `test_the_exemption_window_is_derived_from_the_same_backoff_value`,
  `test_the_wait_and_the_licence_come_from_one_window_and_cannot_drift`,
  `test_the_governor_default_clock_is_the_shared_epoch_clock`,
  `test_the_two_marker_sets_do_not_overlap`,
  `test_the_resilience_classifier_agrees_on_the_action`.
- `tests/test_scheduler_integration.py test_model_router.py
  test_difficulty_approval.py test_ensemble.py test_ceiling14_resilience.py` ->
  **168 passed** (incl. the approval-park/hang interaction).
- `tests/test_config_trace_state.py test_stubs_and_deps.py test_modes.py` ->
  **123 passed**.
- `tests/test_recovery_steering.py test_tracing.py test_evals_run.py
  test_evals_tasks.py` -> **141 passed**.
- `tests/test_orchestration.py test_agent_kernel.py test_agent_loop.py` ->
  **128 passed**.
- `tests/test_e2e_run_task.py` -> **28 passed in 906.85s** (REAL Docker sandbox
  and verifier). The budget-region edit did not disturb the real loop.
- `tests/test_ceiling_r2_13_capability_routing.py` -> **60 passed**, run AFTER
  this round rewired the governor onto R2-13's `estimate_cost`.
- `python -m evals.run --check` -> **14/14 CLEAN**.
- `python -m evals.run --quick` -> **40/40, verdict CLEAN, 0 regressions**
  (5 tasks x 8 arms through the REAL loop and the REAL Docker verifier).
- `ruff check` clean on every owned/edited file; `ruff format` applied to the
  two new files only. `python -m compileall -q runtime harness` clean.
- **NOT run / not claimed:** no live-provider lane, no full Docker prompt
  matrix, no `runtime.stress` / `runtime.soak` / `runtime.abuse` run this
  round. The adversarial `overshoot` scenario in `runtime/abuse.py` is the
  natural regression proof for §3 and was **not** re-run — its measured 9.28x
  figure in that file is the PRE-round number and now overstates the bound.

## VEX-CEILING-R2-13 — capability registry, calibrated routing, and one honest negative (2026-09-27)

**Read this before touching `runtime/model_router.py` or
`runtime/difficulty.py`.** Three files changed, one new file each in `evals/`,
one comment block in `harness/config.py`. No Boundary 0-5 signature changed.

### 1. `runtime/model_capabilities.py` is now the ONE capability authority

It already resolved context windows. It now also owns **tool-calling support,
reasoning-content support, streaming support, and PRICE** — one row per
(provider, model) in one registry, so a cost report and a routing decision can
never disagree about the same model.

| concept | surface |
|---|---|
| closed price vocabulary | `PRICE_STATES = (priced, free, unpriced)` |
| the one price table | `MODEL_PRICES` (re-exported as `model_router._PRICES`, same object) |
| effective rate + origin | `declared_rates`, `price_of` |
| the row | `ModelCapability` (frozen; `to_dict`) |
| the cost answer | `CostEstimate` (frozen; `cost_usd` + `price_state` + `priced`) |
| declarations | `register_capability`, `unregister_capability`, `reset_capability_registry` |
| reads | `lookup_capability`, `capability_of`, `unknown_capability`, `known_capabilities` |
| the gate | `screen_candidates` -> `ScreenResult`, `ToolUseRefusal` |
| refusals | `REFUSAL_REASONS`, `CapabilityRoutingRefused`, `CapabilityError` |

**Three rules this module exists to enforce, in case you "simplify" one:**

1. **A model with no price row is `unpriced`, never `$0`.** The price ladder
   picks the cheapest tier by comparing numbers, so an absent row compared as
   `0.0` makes an unpriced model the CHEAPEST one and the cost report then
   under-reports in the direction that spends money. `free` is reachable only
   by a DECLARED `(0.0, 0.0)` row.
2. **An unknown capability is `None`, never `False`.** `supports_tools=None`
   means nobody described the model. Treating it as `False` would refuse to
   route the entire world; the `capability_strict_tools` key is the opt-in for
   the strict reading.
3. **A malformed declaration RAISES; an absent fact does not.** A negative
   rate or a non-positive window is a `CapabilityError` naming the field. A
   missing price pair is accepted and recorded as `unpriced`. A capability
   declaration that cannot be understood is a refusal, never a default.

`lookup_capability` has four rungs, and the THIRD one is load-bearing: the
exact `(provider, model)`, then `("", model)`, then **any** registered row for
that model name, then the built-in. The provider-agnostic rung exists because
`estimate_cost` is handed a model and no provider — without it an
operator-declared rate was invisible to the cost report. That was a real
defect this round found and fixed (`declared_rates`).

### 2. The router gate — opt-in by KEY PRESENCE, and it can REFUSE

```
runtime.model_router.call_model(...)
    -> target = _resolve_target(...)                    # unchanged
    -> receipt = _capability_screen(target, ctx, ...)   # ONE delegation point
         key absent  -> receipt says enabled=False, everything below unchanged
         key present -> screen_candidates(...) or raise
    -> resilience pipeline (Ceiling-14, unchanged)
    -> the pre-existing dial, byte-identical
```

`_CAPABILITY_CONFIG_KEYS` = `capability_gate`, `capability_allow_unpriced`,
`capability_strict_tools`, `capability_tool_driven`, `model_capabilities`.
The test is `any(key in ctx)`, so `capability_gate: False` still opts IN — the
operator wrote the key and wants the pipeline with its receipts.

**`harness/config.py` deliberately has NO `DEFAULTS` entry for any of them,
not even `None`.** A `DEFAULTS` entry is merged into every task config and
every eval arm, and the switch is PRESENCE, so an entry — `None` included —
would switch the gate on for every run in the project and start refusing
unpriced targets in tasks that never asked for it. `harness/config.py` carries
a comment block saying so, and
`test_the_shipped_default_configuration_cannot_switch_the_gate_on` pins it.
`difficulty_features` DID get a `None` entry because it is a VALUE check.

Refusal vocabulary (`REFUSAL_REASONS`): `unpriced_model`,
`tool_calling_unsupported`, `tool_calling_unverified`,
`no_capable_alternative`. Every refusal is a VALUE with a model, a reason and
a human detail, published on the ledger row (`capability_gate`,
`capability_refusal_count`) and in the `model_routed` trace event.

Two design points worth keeping:

* **An exclusion ESCALATES.** The candidate list is the router's choice first,
  then the remaining tier-table entries in ASCENDING difficulty order, so a
  tool-incapable cheap model hands the call to a more capable tier rather than
  to a cheaper-but-also-incapable one.
* **One documented exemption to the unpriced refusal:** a model the CALLER
  named explicitly (call-level `model`, ctx `model`, `provider_profile.model`).
  The prompt is to refuse to *route*, and a pinned model is not a routing
  decision. Recorded as `explicit_unpriced_bypass`, never silent, and the TOOL
  constraint still applies to an explicit model.

`tool_driven` is decided from the CALL (`tools` non-empty) plus
`capability_tool_driven` for a loop-level declaration. A plain completion is
not gated on tool support.

### 3. `reasoning_content` (the R2-14 protocol) — implemented, pinned, filed

Three named outcomes replace one ambiguous message, and all three still RAISE,
so no completion semantics changed:

| condition | recorded `stop_reason` | ledger |
|---|---|---|
| reasoning present AND `finish_reason == "length"` | `length` | `truncated_reasoning` (a budget problem) |
| reasoning present, not length-truncated | the provider's reason | `empty_response_reasoning_only` |
| no reasoning, no content, no tool calls | the provider's reason | `empty_response` |

`_extract_reasoning` reports THAT reasoning was there and how much, from the
message field or `usage.completion_tokens_details.reasoning_tokens`. **It
never returns the reasoning as the answer** — a hidden trace is not a reply,
and substituting it would launder a truncated turn into a
successful-looking one. `supports_reasoning` in the registry documents the
per-model shape.

**R2-14 owns the protocol DOCUMENT; this is the implementation.** If R2-14
names the outcomes differently, change the three slugs here and in the test —
the ledger keys (`reasoning_content_present` / `reasoning_chars` /
`reasoning_tokens`) and the "never the answer" rule are the part to preserve.

### 4. The structural difficulty predictor — built, measured, NOT shipped

`runtime/difficulty.py` gained `repo_shape`, `symbol_fan_in`,
`target_test_exists`, `classify_bug_class`, `structural_features`,
`score_to_structural_hint`, `predict_structural`, `group_holdout_split`,
`compare_predictors`, `BUG_CLASSES`, `BUG_CLASS_WEIGHTS`,
`MIN_HOLDOUT_HARD_LABELS`, `MAX_WALK_ENTRIES`, `MAX_FANIN_MODULES`.

The incumbent heuristic, its bands, its `difficulty_calibration.json`
override, and `predict_difficulty`'s signature/return shape are UNCHANGED, and
the eval matrix's ablation path through it still runs.

**It is reachable only by `Task.config["difficulty_features"] = "structural"`.**
`"auto"` resolves to the incumbent because
`model_router._structural_calibrated()` reads
`runtime/difficulty_structural_calibration.json`, which nothing writes unless
`evals.difficulty_holdout --apply` sees `ship: true`. No such file exists.

### 5. THE MEASURED RESULT, and it is a "no" on shipping

`python -m evals.difficulty_holdout --logs-root logs --apply`, over this
machine's real accumulated run history:

| | rows | acc (easy-or-hard) | missed esc | false esc |
|---|---|---|---|---|
| train (90) legacy | 90 | 0.9000 | 7 | 2 |
| train (90) structural | 90 | 0.9222 | 7 | **0** |
| **holdout (17) legacy** | 17 | 0.8235 | 1 | 2 |
| **holdout (17) structural** | 17 | **0.9412** | 1 | **0** |

107 real adaptively-routed fix runs, 65 bug groups, the documented strict label
policy (verifier-refused failure = hard, success = easy, endpoint deaths
excluded), grouped split so one bug never straddles it.

**The structural predictor WON the held-out split and is still not shipped.**
The split carries **1 hard-labelled observation** against
`MIN_HOLDOUT_HARD_LABELS = 3`. A win on one hard case is one lucky prediction,
so `compare_predictors` reports `winner="structural"`, `decidable=True`,
`sample_adequate=False`, `ship=False`, and the honesty line says
`promising-but-unproven`. `--apply` wrote nothing.

**Feature coverage, per feature, because the aggregate would have hidden it:**

| feature | measured | why |
|---|---|---|
| `repo_size` | 107/107 | every accepted task's repo path resolved and exists |
| `test_density` | 107/107 | same |
| `bug_class` | 107/107 | every accepted task's issue text resolved |
| `target_test_missing` | **18/107** | runs that rely on test autodetection declare no `target_test`; `baseline_verify.target_test` was `null` in all 89 others |
| `fan_in` | **0/107** | run history records touched FILES and carries no per-task touched-SYMBOL list, so the feature is UNMEASURED, not measured-as-zero |
| `multi_module` | 0/107 | no run records its expected changed files at predict time |

**The design-leakage caveat, which bounds the whole claim:** the structural
feature family was chosen AFTER reading this project's own documented bug
corpus (`runtime/AGENTS.md` names the real hard bugs — backoff-race,
parse-comma, slugify-case, inflect/boltons ordinal-teen). The bug-class probes
and the fan-in signal were picked knowing which bug shapes this project has
hit. So this is a valid test of the structural BANDS and a WEAK test of the
feature DESIGN. It is in the report's `honesty` list and should be in any
summary of this round.

### 6. The capability ablation — `evals/routing_capability.py`

`python -m evals.routing_capability` -> **CLEAN, 3 arms, 5 cases, 21 checks, 0
regressions**. Arms: `price_only` (no capability key — the pre-R2-13
behaviour), `capability`, `capability_strict_tools`. The router runs for real;
only the model RESPONSES are mocked. The fictional `acme/*` models are
registered through the real registry.

Why it is NOT an arm in `evals/run.py`: that suite pins
`adaptive_routing: False` and `use_mock_provider: True`, so a capability arm
there would compare two identical configurations and publish a vacuous "no
delta". The honest home for this comparison is its own driver.

### 7. Verification actually run (this tree, `-p no:randomly`)

- NEW `tests/test_ceiling_r2_13_capability_routing.py` -> **60 passed**
  (host-only, no Docker, no network, no credential).
- `tests/test_model_router.py tests/test_difficulty_approval.py
  tests/test_prompt_cache_cost.py tests/test_evals_run.py tests/test_evals_tasks.py
  tests/test_config_trace_state.py` -> **128 passed**.
- `tests/test_ceiling14_resilience.py tests/test_ensemble.py
  tests/test_analyze_history.py tests/test_scheduler_integration.py` ->
  **172 passed**.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `python -m evals.routing_capability` -> CLEAN, 21 checks, 0 regressions;
  report at `logs/evals/r2-13-routing-capability/`.
- `python -m evals.difficulty_holdout --apply` -> NOT shipped; report at
  `logs/evals/r2-13-difficulty-holdout/`.
- `ruff check` clean on every file this round owns or created.
  `runtime/model_router.py` still carries its two PRE-EXISTING `F401`s
  (`typing.List` / `typing.Tuple`, from the Ceiling-10 streaming edit); they
  were left alone rather than "fixed" inside a shared file.

### 8. Not run / not implemented / honest

- **No live-provider lane was run and no credential was read.** Everything
  here is the offline mock provider plus fake provider responses. The
  capability measurements are about WHICH MODEL GETS SELECTED and what the
  cost report says — not model quality, latency, or tokens.
- **No Docker lane was run.** Nothing in this round needs the verifier.
- **The structural predictor is NOT enabled.** Nothing in the router turns it
  on; it is reachable only by an explicit `difficulty_features` value.
- **`fan_in` is implemented and measured, and unmeasured in practice** on this
  history (0/107). It is not a placeholder: `symbol_fan_in` is tested against
  a real import graph, but a producer must emit per-task symbols for the
  feature to carry signal.
- **The built-in capability table covers the models this project routes to**
  (the `DEFAULT_MODEL_TIERS` entries plus the ablation's proxy tiers). A
  bring-your-own model resolves to the honest UNKNOWN row, which the gate
  treats as eligible-but-unpriced rather than as incapable.
- **The `context_window` ladder, `SYNTHETIC_PROVIDERS`, and the per-endpoint
  cache are UNCHANGED**; R2-08's note about `budget.py` string-coupling to the
  `"fallback"` rung still stands and is still not fixed here.

### 9. Cross-terminal requests

1. **Producers of per-task state (`harness/core.py`, `harness/agent_loop.py`):**
   emit the task's touched SYMBOLS, not just files, and the expected changed
   files. Two of the five features the R2-13 brief named (`fan_in`,
   `multi_module`) are unmeasurable without them, and the held-out report's
   `feature_measured` will show `0/107` until they land. Either a
   `state.json` additive key (`touched_symbols: [...]`, `changed_files:
   [...]`) or a `task_start`/`plan` trace field works; the driver reads
   whatever the trace carries today (`symbols` on a trace row) and the
   structural predictor reads `changed_files` / `touched_symbols` from its
   context. `shared/types.py` would need a field for the durable version —
   that is another owner's file and was not edited.
2. **R2-14 owner:** this round implemented the `reasoning_content` handling
   the R2-13 brief refers to. If R2-14 defines the outcome slugs
   differently, reconcile the three literals in `model_router.call_model` and
   the four `test_reasoning_*` tests. The "reasoning is never the answer" rule
   and the ledger keys are the part to preserve.
3. **`harness/agent_kernel/budget.py`:** unchanged, still string-coupled to
   `context_window_source == "fallback"`. If that rung is ever renamed, the
   budget authority shrinks for unknown models. A named `SOURCE_FALLBACK`
   constant would remove the coupling. Not fixed here (R2-13 did not own it
   and did not need to touch the file).
4. **`evals/AGENTS.md` owner (NOT edited by this round — that file belongs to
   another prompt):** two new modules landed in `evals/` and `evals/AGENTS.md`
   does not mention them yet. Please add a section, or forward this one:
   - **`evals/routing_capability.py`** — the capability-selection ablation.
     `python -m evals.routing_capability [--arms ...] [--cases ...] [--out DIR]
     [--json]`. 3 arms x 5 fixed cases through the REAL router with the
     offline mock provider; 21 checks; exit 0 CLEAN / 2 on a regression or a
     bad selection. Arms are `price_only` (no capability key — the pre-R2-13
     behaviour), `capability`, `capability_strict_tools`. It is deliberately
     NOT an entry in `evals/run.py::ARMS`: that suite pins
     `adaptive_routing: False` and `use_mock_provider: True`, so a capability
     arm there would compare two identical configurations and publish a
     vacuous "no delta". If the suite ever gains a real routing lane, this is
     the arm to move there.
   - **`evals/difficulty_holdout.py`** — the held-out predictor comparison.
     `python -m evals.difficulty_holdout [--logs-root DIR] [--holdout-frac F]
     [--seed N] [--out DIR] [--json] [--apply]`. Exit 0 when the comparison
     ran (whatever the verdict), 2 when the logs root is absent, 3 when the
     structural challenger actually WON and is eligible to ship. `--apply`
     writes `runtime/difficulty_structural_calibration.json` ONLY on
     `ship: true`; on this repository it does not, and the reason is in the
     report's `honesty` field. It reads the documented public trace format
     (`{"kind": ..., "data": {...}}`) for the per-task issue text and declared
     target test — no private cross-module import.
   - If you also wire `--suite` in `evals/run.py`, note that neither driver
     needs Docker, a provider, or a credential, so both are safe to add to the
     host `--check` lane.

## VEX-CEILING-14 — local-first, provider resilience, offline mode, privacy (2026-09-26)

Gap G39 ("no first-class local model tier or provider outage drills") is
closed. Five new modules plus **one** surgical delegation point in
`model_router.py`.

### What is new

| module | owns |
|---|---|
| `provider_resilience.py` | per-provider circuit breaker (closed/open/half-open), bounded fallback chain, idempotency-aware retry classification |
| `local_models.py` | the local tier: profile, role routing, context-window probe + cache, local-vs-frontier token/cost split |
| `privacy_policy.py` | per-task policy for what may leave the machine, provider ZDR metadata, redaction of the outgoing messages |
| `offline_mode.py` | the offline indicator, the no-egress gate, the durable deferral queue and its drain |
| `provider_gateway.py` | the pipeline that composes the four, and the drop-in Boundary-2 entry point |

### The one delegation point

```python
runtime.model_router.call_model(...)          # unchanged signature
    -> if _resilience_requested(ctx):          # ANY Ceiling-14 key present
         _resilient_call(...)                  # -> runtime.provider_gateway
    -> else: the pre-existing code path, byte-identical
```

`_resilience_requested` is a **KEY** test, not a value test. Nothing in
`runtime/config.py::DEFAULTS` carries a Ceiling-14 key, on purpose: a value
in `DEFAULTS` is merged into every task config, which would silently switch
every task (and every ablation arm) onto the new path. "Absent" is a
meaningful state that means *unchanged behavior*, and the pipeline carries
its own internal defaults for its knobs.

**A streamed call is not failed over.** `stream=True` runs only the GATE
half (`provider_gateway.enforce_stream_gate`: redaction, offline, privacy)
and then continues into the router's own streaming dial, because a partially
consumed response must never be replayed against a second provider as a
second charge. The gate still applies, so `stream=True` is **not** a way to
bypass redaction or the offline/privacy refusals. Both halves are
regression-pinned (`test_a_streamed_call_still_runs_the_gate`).

### The pipeline, in order, and why

1. **Redact** first, because every later receipt describes "what was sent"
   and a receipt about unredacted content is a leak of its own. When nothing
   needed redaction the ORIGINAL message objects are returned, so the bytes
   on the wire (and the prompt-cache prefix digest, from Ceiling 09) are
   unchanged for the common case.
2. **Resolve candidates**: the primary from the router's normal precedence
   (with the local-first substitution), then a BOUNDED chain —
   `provider_fallbacks` if given, else the remaining `model_tiers` entries in
   declaration order. Bounded by `provider_fallback_max` (3); the primary is
   never dropped.
3. **Screen** each candidate: offline first, then the privacy policy, then
   the breaker. A refused candidate is **recorded** (ledger `outcome:
   "skipped"` + a stable reason) and skipped, not silently dropped.
4. **Dial** with the router's own bounded retry loop, then classify the
   outcome. A *provider fault* (connection/5xx/rate-limit/auth) counts
   against the breaker and moves to the next candidate. A **rejected request
   is not an outage**: a 400 does not trip the breaker, because taking a
   healthy endpoint out of rotation over one bad request is a self-inflicted
   outage.

### Fail-closed shapes

- every candidate refused by privacy -> `PrivacyPolicyBlocked` (a
  `PermissionError` subclass, carrying every decision)
- every candidate refused by offline -> `OfflineEgressBlocked`
- every candidate tried and failed -> **the last provider exception**,
  re-raised unchanged when there is only one candidate. With no fallbacks
  configured this module is transparent, including the exception object the
  caller sees.

### Honest limits (not silent)

- **The breaker is per-process.** A worker subprocess gets its own registry,
  so a 50-way fan-out does not share one breaker's state. A cross-process
  breaker belongs in the scheduler, which does not own provider calls.
  `provider_resilience.snapshot()` makes the per-process view explicit.
- **`half_open` admits exactly one probe.** A second concurrent caller is
  refused. A half-open breaker admitting 50 probes is just an open breaker.
- **Zero-data-retention is a claim, not a guarantee.** An unregistered
  provider is `zero_data_retention="unknown"` and every strict policy
  refuses it; `zdr_kwargs` returns only a parameter the provider's own API
  documents, and the ledger records `zdr_requested` / `zdr_parameter` so a
  reader can see whether a flag was actually sent or only claimed.
- **Redaction cannot be turned off.** `privacy_redact: false` is accepted for
  explicitness but the `secret` class is stripped from the policy's ceiling;
  a credential is not exfiltrated because a config asked for it.
- **No endpoint means no locality.** `classify_target("openai", None)` is
  `frontier`: a provider name without an endpoint is litellm's own hosted
  default unless the provider itself is a local one. Guessing the other way
  is exactly the lie that makes a cost report wrong.
- **The offline read-only question path is a CONFIG fragment plus a
  boundary, not a switched mode.** `offline_read_only_config()` returns the
  Task.config dict a caller needs; adopting it inside the harness's question
  strategy is a harness-owner integration (see the handoff).

### Durable offline queue

`logs/{task_id}/offline_queue.jsonl`, append-only, one JSON record per line,
`fsync`'d per entry. `pending()`/`drain()`/`health()` derive everything from
the file, so a hard kill loses nothing. Re-enqueue is idempotent per
`entry_id`; a handler that raises marks the entry failed and CONTINUES by
default (one poisonous item must not strand the rest), leaving it pending. A
torn final line from a killed writer is skipped and reported as
`health()["malformed"]` rather than silently eaten.

### Config keys (all opt-in; see `runtime/config.py` for the full block)

`local_model_profile` / `local_model` / `local_provider` / `local_api_base` /
`local_context_window` / `local_first` / `local_first_roles` /
`local_first_decisive`; `provider_fallbacks` / `provider_fallback_max` /
`provider_fallback_across_tiers` / `model_calls_idempotent` /
`circuit_breaker_registry`; `offline` / `offline_allow_local_models` /
`read_only`; `privacy_policy` / `privacy_data_classes` /
`privacy_providers` / `privacy_models` / `privacy_require_zdr` /
`privacy_redact`.

Env equivalents: `NEO_OFFLINE`, `NEO_CIRCUIT_BREAKER_THRESHOLD`,
`NEO_CIRCUIT_BREAKER_RESET_S`, `NEO_SETTINGS_WRITE_ATTEMPTS`.

### Verification actually run

- `python -m pytest tests/test_ceiling14_resilience.py -q -p no:randomly` ->
  **68 passed**, including a REAL-Docker twin of the required outage test
  (primary provider 503 on every attempt -> bounded fallback -> `success`
  with clean target + regression evidence, and the original repository
  byte-identical).
- `tests/test_model_router.py tests/test_prompt_cache_cost.py
  tests/test_tool_protocol.py` -> **92 passed**.
- `tests/test_scheduler_integration.py tests/test_difficulty_approval.py
  tests/test_ensemble.py tests/test_stubs_and_deps.py` -> **79 passed**.
- `tests/test_ceiling_security.py tests/test_security_regressions.py
  tests/test_tracing.py` -> **92 passed, 4 skipped** (Windows symlink
  privilege cases, not passes).
- `tests/test_cli_config.py tests/test_cli_onboard.py
  tests/test_cli_auth_release.py tests/test_cli_plugins.py
  tests/test_cli_connectors.py` -> **211 passed, 2 skipped**.
- `python -m evals.run --check` -> **14/14 CLEAN**.
- **No live-provider lane was run.** `tests/test_provider_smoke.py` reports
  **5 skipped** (no `OPENAI_API_KEY`, placeholder/absent Anthropic
  credential, ollama not ready). Not a pass, and not reported as one. No
  credential was read, printed, or retained.
- `ruff check` clean on every file this round created or owns.
  `runtime/model_router.py` carries **two pre-existing `F401`s**
  (`typing.List` / `typing.Tuple` unused) that came from the Ceiling-10
  streaming edit, not from this round; they were deliberately left alone
  rather than "fixed" inside another terminal's file.

### Two real defects this round found (both in shared code, both fixed)

1. **Lost update under concurrent config writes** (`cli/neoconfig.py`).
   `set_tier_key` sampled `existed = p.is_file()` BEFORE taking the lock and
   then branched on that stale answer, so a writer that arrived while
   another was creating the file wrote its own value as if the file were
   still absent — deleting the other writer's keys. The mutator now receives
   `(data, existed)` sampled INSIDE the lock. Found by the required
   concurrent-write test (4 of 24 keys lost before the fix, 0 after).
2. **A lock holder was never released across top-level acquisitions**
   (`cli/neoconfig.py`). The re-entrancy holder was reset to `count: 0`
   instead of cleared, so the NEXT top-level acquisition on the same thread
   looked re-entrant and skipped the real lock entirely. Found by the
   held-lock test.

## VEX-CEILING-06 — planning, subagents, worktrees, and orchestration (2026-09-26)

This section is the authoritative handoff for the ceiling-06 round. It
supersedes nothing above: the Terminal 08 bounded-orchestration section below
is still the description of the DAG/claims/approvals/recovery base, and this
section is what was added on top of it.

### 1. Dynamic planning — `runtime/planning.py` (NEW)

Plan state is a first-class, versioned object. A plan is
`[PLAN_STEP_FLOOR=2 .. max_steps]` steps, `max_steps` clamped into
`[2, PLAN_STEP_CEILING=12]` by `clamp_max_steps`.

- **Planned by behavior AND files.** `PlanStep.behavior` is mandatory (an
  empty behavior is a `PlanError`); `files` is mandatory for `kind="edit"`
  (`PlanError`, message names the contract). A target with a behavior and no
  file becomes a `kind="explore"` discovery step rather than a fake edit.
  More targets than the cap are distributed across the cap — no target is ever
  silently dropped — and a `kind="verify"` step is appended so every plan ends
  on a checkable outcome (this is also what lifts a single-file task to the
  two-step floor).
- **Replan is driven by measured outcomes, not prose.**
  `record_step_outcome(...) -> ReplanDecision` and the three stable reason
  slugs are `constraint_violation` > `turns_exhausted` > `no_files_touched`.
  `apply_replan` supersedes the offending step, inserts a re-scoped
  replacement, bumps `version`, and appends a machine-readable `replans`
  record (`step_id`, `replacement_step_id`, `reason`, `from_version`,
  `to_version`, `ts`). At the cap the replacement takes the superseded slot
  rather than growing the plan.
- **A completion must be real:** a step marked `completed` with an empty
  `files_touched` is demoted to `failed` in `record_step_outcome`.
- Persistence is `save_plan_state`/`load_plan_state`
  (`{"schema_version": 1, ...}`, atomic write, unknown version fails closed),
  `render_plan_block` injects a bounded prompt block, and `plan_digest` is
  for identity/resume checks. `plan_from_model_steps` adopts a model-authored
  plan (the `plan` control tool's `steps`) under the SAME contract.
- Config: `max_plan_steps` (12), `plan_step_turn_budget` (12).

### 2. Subagents — `runtime/subagents.py` + `harness/agent_kernel/subagents.py` (NEW)

- **Definitions are versioned files**, Markdown frontmatter or a TOML
  `[agent]` table, in `<repo>/.neo/agents/**` (project) then
  `~/.config/neo/agents/**` (global). The declared `name` wins over the file
  name. A definition that (a) requests a tool its `runtime.roles` profile does
  not expose, (b) requests the `task` tool, (c) declares an unknown role, (d)
  declares a non-semver or a different major, or (e) names an unknown model
  tier is **refused at load** and reported in `AgentRegistry.diagnostics` — one
  broken file never disables the others.
- **`max_children` is the recursion bound.** A definition with
  `max_children: 0` cannot create children at all. Role profiles in
  `runtime/roles.py` are UNCHANGED, so no closed profile exposes a spawn tool
  by default; a subagent's restricted tool set is the definition's `tools`
  (a subset of its role), and the spawner — not the model — owns recursion.
- **The `task` tool is a real bounded spawn** through
  `harness/agent_kernel/subagents.py::dispatch_task_tool`:
  - in-process (the run owns the orchestrator) → `SubagentSpawner.spawn`
    returns an admitted node id;
  - a child agent (its own process) → the request is written to the
    workflow's durable `SpawnRequestStore` and admitted by the parent
    orchestrator at its next tool boundary (`_drain_spawn_requests`);
  - neither bound → `error_kind="no_runtime"`, an honest refusal.
  Every call is still journaled as `control_intent`, so an intent is auditable
  whether it was admitted, queued, or refused.
- **Admission checks, in order**: definition may spawn → per-parent fanout →
  depth → graph node caps → visible concurrency → per-child cost → total
  budget. Every refusal is a `SubagentAdmission` VALUE with a reason (the
  model's turn budget survives it); `Orchestrator.spawn_child` re-checks the
  DAG caps and is the single admission point.
- **Bounded summaries**: `ChildSummary.render()` caps at
  `subagent_summary_max_chars` (2048) and degrades fields in a fixed order,
  so parent context growth per child is bounded by construction. The spawn
  receipt also carries the visible concurrency cap and the live child count.
- Config: `subagent_max_depth` (2), `subagent_max_children_per_parent` (4),
  `subagent_max_concurrent` (4), `subagent_max_spawn_requests` (16),
  `subagent_max_child_turns` (12), `subagent_max_child_cost_usd` (2.0),
  `subagent_max_total_cost_usd` (10.0), `subagent_summary_max_chars` (2048),
  `subagent_default_agent` ("").

### 3. Worktree isolation and AST symbol claims

- **`neo worktree new|list|go|rm`** and **`neo fix --worktree NAME`**
  (`cli/commands.py` + the additive `cli/main.py` subcommand). `--worktree`
  materializes the isolated checkout BEFORE any model call and repoints
  `Task.repo_path` at it; a dirty source checkout fails closed (exit 2).
- **Claims are symbol-scoped.** A scope is `path` (whole file) or
  `path::symbol` (one AST symbol). `runtime/symbols.py` extracts Python
  symbols with stdlib `ast` (functions, classes, and their methods with real
  line ranges), scans JS/TS conservatively, and returns nothing for other
  languages — which forces a whole-file claim rather than a guess. A whole
  file scope is a whole-file claim (the node owns the file end to end); a
  symbol scope is precise, so a node that claimed `mod.py::handler` cannot
  edit a sibling symbol or the module level in that file.
- **`unclaimed_symbol_edits(patch, claims, root=...)`** is the pre-merge
  guard: a patch that touches an unclaimed file, an unclaimed symbol, or
  module-level code in a symbol-scoped file is REFUSED (the merge is recorded
  with the offending `path/line/symbol/reason`), never applied.
- **Claims have leases.** `ClaimStore` records `heartbeat_epoch`, `pid`, and
  `lease_s` (`claim_lease_s`, 120s). `reclaim_stale(live_owners=...,
  dead_owners=...)` releases exactly the owners that are provably not running;
  a live owner is protected even if its lease lapsed. `run()` reclaims at
  start, and `_reclaim_node_claims` reclaims a node's OWN stale claim before
  it re-acquires (a crashed predecessor cannot rob a live sibling's scope).
- **Merges are serialized and verified.** `merge_children()` takes one merge
  lock, walks the nodes in dependency order, and applies each child's patch
  to ONE integration worktree (`worktrees.ensure_integration()`,
  `INTEGRATION_NODE_ID="integration"`) after the claim guard passes. Every
  merge — including a refused or empty one — then runs `_verify_merge`, so
  "verification ran for every merge" is a countable fact. The default gate is
  offline and deterministic (`git diff --check` plus a `compile()` of declared
  changed files); a workflow that declares `merge_verification.test_command` /
  `target_test` additionally runs the real `execution.verify.verify` against
  the integration tree. A node WITH children is reported as `aggregate` rather
  than re-merged: its worktree already contains its children's patches.
- `WorkflowLimits` additions: `max_children_per_parent` (4),
  `max_dynamic_nodes` (8), `claim_lease_s` (120), `symbol_claims` (True),
  `merge_enabled` (True), `merge_verify_timeout_s` (300),
  `coordination_budget_fraction` (0.10).

### 4. Orchestration integration

- **One orchestrator.** `SubagentSpawner` never executes anything; it resolves,
  admits, and hands the node to `Orchestrator.spawn_child`, which is the single
  admission point. Spawned nodes are persisted in `state.metadata["live_spec"]`
  and re-inflated on `resume=True`, so a restart keeps the graph.
- **Every child run is traced, costed, and verifiable.** Child packets gained
  `claims` (the node's own scope, for the worker and for audit) and
  `config["orchestration_spawn_dir"]`. Cost reservation/`spent_cost_usd` are
  unchanged and every admission is checked against the budget. Handoffs are
  the existing `HandoffSummary`; the parent-facing view is the bounded
  `ChildSummary`.
- **Coordination overhead is measured, not asserted**:
  `coordination_report()` returns samples, total seconds, p95 seconds, wall
  clock, the share of wall clock, and `within_budget` against
  `coordination_budget_fraction`.

### Verification actually run (this round)

- `python -m pytest tests/test_ceiling06_orchestration.py -q -p no:randomly`
  → **36 passed** (2m44s), including the six prompt-required proofs (six-file
  plan ≥ 6 steps; turn-exhausted-with-zero-files → replan; three children →
  summaries < 2KB each; five-way parallel → zero lost edits; crashed child →
  claims released; post-merge verification for every merge) and a REAL
  Docker-backed `neo fix --worktree iso-1` run that exits 0 with the original
  repository byte-identical.
- `python -m pytest tests/test_cli.py tests/test_cli_command_system.py
  tests/test_cli_release.py -q -p no:randomly` → **141 passed**.
- `python -m pytest tests/test_tool_protocol.py tests/test_workspace_security.py
  -q -p no:randomly` → **82 passed** (the `task` catalog change keeps kernel
  catalog parity).
- `python -m pytest tests/test_agent_kernel.py tests/test_scheduler_integration.py
  -q -p no:randomly` → **84 passed, 3 failed**. The three failures are the
  documented host-load hazard, NOT this round: on this machine
  `import litellm` measures **32-34s**, and both failing tests kill a worker at
  the default 30s `hang_heartbeat_stale_s` (scheduler journal evidence:
  `{"event": "hang_timeout", "signal": "state_stale", "state_age_s": 30.07}`).
  Controlled probe: the identical fake-harness task with the documented
  `hang_heartbeat_stale_s=300` mitigation produced its expected 5-record
  ledger on `gpt-4o-mini`. They are not counted as passes.
- `python -m pytest tests/test_orchestration.py -q -p no:randomly` →
  **15 passed, 4 failed**. All four failures are `TestOrchestratorSubprocess`
  cases whose child wall-clock budget is 3.0s; a measured child needs ~30s on
  this host for the same `import litellm` reason (a manual child run returned
  rc=0 with a correct result in 30.55s). Same environmental class, not
  attributed to this round, not counted as passes.
- Scoped `ruff check` clean on every owned/disclosed file; `python -m
  compileall -q runtime harness/agent_kernel cli` clean; `graphify update .`
  run at close.

### Not selected / not implemented (honest)

- **No live-provider lane.** No credential was inspected, requested, or
  retained. Every model interaction in the new tests is an injected executor,
  a scripted model, or the mock provider.
- **The Docker lane was selected for exactly one test** (the isolated
  `neo fix --worktree` run) and the daemon answered (28.5.1). The Docker-backed
  verifier inside `execution.verify` is wired into `_run_merge_verifier` but
  was **not exercised** by a test in this round: the merge tests use the
  default offline gate. A workflow that configures `merge_verification`
  with a test command is the untested path — stated, not implied.
- **`neo worktree` covers `new|list|go|rm` for the managed root only.** There
  is no `prune` beyond `WorktreeManager.prune()` and no
  `merge`-from-the-CLI surface; merging is an orchestrator phase.
- **Symbol extraction is Python-complete, JS/TS-conservative.** A file in any
  other language can only be claimed whole-file, so two units cannot split one
  such file by symbol. Documented, not hidden.
- **Plan state is not yet injected into the harness planner's own prompt.**
  `render_plan_block` is the seam and is tested; wiring it into
  `harness/prompts.py` belongs to the harness owner (that file is being edited
  by another terminal).

## VEX-CEILING-09 — prompt caching, cost, and latency (2026-09-26)

**Reduce cost without weakening verification.** Three new modules and one
extended boundary. `runtime/model_router.py` is shared with in-flight
Terminals 06/14/16 work (local models, streaming, provider resilience,
offline mode, privacy policy) — my changes are the cache plan/receipt block
inside `call_model`, the added ledger/trace keys, the `_fallback_cost`
`cached_input_tokens` argument, and `get_cache_summary()`.

### What landed

- **`runtime/prompt_cache.py` (new)** is the single owner of *where a cache
  breakpoint sits* and *what counts as a hit*. It is pure: it digests,
  decides the provider parameters, and folds a usage payload into a
  `CacheReceipt`. It never calls a provider.
  - `frozen_prefix` is deliberately **one** system message, not the whole
    leading system run. Over-including a volatile system message silently
    destroys every future cache hit while looking healthy in the receipts;
    under-including costs a few tokens. `breakpoint_index` lets a caller say
    "my prefix is longer".
  - `prefix_digest` covers the frozen messages **plus the normalized tool
    schemas**, so a catalog change is a real prefix change and shows up as
    exactly one invalidation.
  - `normalize_tool_schemas` sorts, so a reordered-but-equivalent catalog is
    NOT reported as an invalidation.
  - `cache_status` vocabulary: `hit` | `partial` | `creation` | `miss` |
    `unsupported` | `unreported`. **`unreported` is the honest answer when a
    provider returns no cache field at all** and is excluded from the hit rate
    in both directions — counting it as a hit would overstate efficiency and
    counting it as a miss would overstate savings. `miss` is only claimed when
    the provider demonstrably speaks the cache protocol
    (`cache_tokens_from_usage` distinguishes "field present with value 0" from
    "field absent" via `_has_field`).
  - `PromptCacheLedger` is context-local (so concurrent tasks never share one)
    and reports `cache_hit_rate`, `cached_input_tokens`,
    `cache_creation_tokens`, `cache_invalidation_count`,
    `cache_distinct_prefixes`.
- **`runtime/model_capabilities.py` (new)** resolves a context window and
  caches it per `(api_base, model)` — two endpoints serving the same model
  name are not the same capability. Ladder: injected probe -> litellm
  `get_model_info` -> local table -> `FALLBACK_CONTEXT_WINDOW` (8192).
  **An unknown window is never zero**, a probe that raises or returns 0 is a
  refusal rather than an answer, and the `source` string always says which
  rung produced the number. This module makes **no network call**: a live
  probe must be supplied by the caller (`context_window_probe` config).
  litellm's provider-list banner is captured and discarded so it cannot corrupt
  a `neo --json` document.
- **`runtime/model_router.py`**: `call_model` computes the plan once per call
  (so a FAILED call still records what it would have cached), sends
  `cache_control: {"type": "ephemeral"}` to Anthropic-family endpoints only,
  and annotates the last tool schema as a second breakpoint. **OpenAI-
  compatible and Google endpoints get no parameter** — an unknown key is a
  hard 400 on most gateways — but their prefix is still digested, so their
  receipts still work. Every ledger row, every `get_last_usage()` record, and
  the unified `model_routed` event now carry the receipt plus
  `context_window` / `context_window_source`. `_fallback_cost` gained an
  optional `cached_input_tokens` argument and prices that portion at
  `DEFAULT_CACHE_READ_DISCOUNT` (Anthropic's published 0.1x), reporting
  `cost_source = "...+cache_discount"` so an estimate is never presented as
  an invoice. New `get_cache_summary()`.
- **Config keys** (additive, task-config driven): `prompt_cache` (True),
  `prompt_cache_min_prefix_tokens` (1024), `prompt_cache_breakpoint`
  (explicit index), `context_window_probe` (callable).

### Where the >90% hit rate is actually realized

- The **daily kernel** path was already prefix-stable by construction
  (Terminal 01's `ConversationMemory` seeds `[DAILY_SYSTEM, user]` once per run
  and only ever extends it), so `[frozen system] + [tool schemas]` is byte
  identical across every turn of a run.
- The **planner** call is already `[PLANNER_SYSTEM, PLANNER_USER]`.
- `harness.prompts.render_step_messages` provides the same property for the
  legacy step loop, and `harness.prompts.STEP_SYSTEM_TEMPLATE` is now
  static-first so the split exists at all. **`harness.core.run_step` was NOT
  switched to it** — see the disclosed blocker below.

### Disclosed blocker: the legacy step loop is not converted

Switching `harness.core.run_step` to `render_step_messages` makes the legacy
step session's prompt cacheable, and I measured exactly what it costs:
`tests/test_batch_docs_lint.py` and `tests/test_webfetch.py` have scripted
models that dispatch on the step SYSTEM message
(`re.search(r"your step is #(\d+) of", system)`) and on
`users[-1].startswith("Begin.")`. Changing the message shape breaks them.

I reverted the `core.py` change rather than editing another terminal's test
pins inside a shared dirty worktree. **`render_step_messages` is implemented,
covered by `tests/test_prompt_cache_cost.py`
(`test_step_prompt_prefix_is_byte_stable_across_steps_and_turns`), and ready
to be adopted** — the conversion is a one-line change at
`harness/core.py` once Terminal 01/10 and the eval authors agree the scripted
dispatch should read the second system message (or the joined system text).
`render_step_system`'s own signature and rendered content are unchanged, so
every existing pin still passes.

### Verification actually run

- `python -m pytest tests/test_prompt_cache_cost.py -q -p no:randomly` ->
  **32 passed** with the Docker daemon UP, including the three real-Docker
  tests (warm-sandbox identity/leak, repeated exec + clean release, and the
  batched-vs-separate read equivalence through the real sandbox).
- `python -m pytest tests/test_prompt_cache_cost.py tests/test_model_router.py
  tests/test_tool_protocol.py -q -p no:randomly` -> **92 passed**.
- `python -m pytest tests/test_model_router.py tests/test_tool_protocol.py
  tests/test_retrieval_tools.py tests/test_editor_prompts.py
  tests/test_recall_unit.py tests/test_multilang_graph.py
  tests/test_code_graph.py tests/test_prompt_cache_cost.py -q -p no:randomly`
  -> **212 passed, 7 skipped** (the skips are the Docker-gated cases from a
  moment when the shared daemon was wedged; they passed in the 32/92 runs).
- `tests/test_modes.py tests/test_stubs_and_deps.py
  tests/test_config_trace_state.py` -> **123 passed**.
- `tests/test_batch_docs_lint.py tests/test_webfetch.py` -> **74 passed**
  (after the shared Docker daemon recovered; a first run failed 10 of these
  with `baseline verify crashed: docker daemon not reachable`, and an A/B probe
  driving the same task with the pre-round step-template order produced the
  identical failure, so the outage is environmental and not this round's).
- `tests/test_agent_kernel.py` -> **47 passed**;
  `tests/test_scheduler_integration.py tests/test_difficulty_approval.py
  tests/test_ensemble.py tests/test_tracing.py` -> **108 passed**;
  `tests/test_cli_slash2.py tests/test_cli.py` -> **58 passed** (the `/cost`
  and `--json` surfaces).
- `python -m ruff check` clean on every file this round created; `ruff format`
  applied to the new files. `compileall` clean; scoped `git diff --check`
  clean.

### Not run / honest blocked status

- **No live-provider lane.** Every provider receipt in the tests comes from a
  fake `litellm.completion` installed through the router's own seam. No
  credential was inspected, printed, or retained. Consequently the **TTFT
  40%+ target is NOT measured** — it needs a real provider's first-token
  timing on a cached prefix, and the fake provider reports token counts, not
  latency. The mechanism that would produce it (byte-identical prefix +
  breakpoint) is in place and its correctness is pinned.
- **Cost per task <= $0.025 on the pinned reference workload is NOT measured**
  for the same reason: the price-table path is exercised in tests, but no real
  provider invoice was produced this round.
- **Inner verification p50 < 20% of full-suite time is NOT addressed by this
  round.** It is a `execution.verify` change (incremental inner verification
  with the full suite as the final gate) and belongs to Ceiling Prompt 08,
  which owns machine-checkable specs and verification intelligence. Calling it
  out rather than half-implementing it.
- `harness/prompts.py` and `harness/retrieval.py` keep their PRE-EXISTING
  whole-file `ruff format` debt (large shared dirty files with parallel
  in-flight edits). Only this round's own hunks were hand-formatted; a
  wholesale reformat would clobber another terminal's work.
- `tests/test_e2e_run_task.py::test_verified_success_state_complete_after_
  exhausted_turns` fails in this tree: its script repeats one command 3x to
  exhaust the turn budget, and Terminal 04's `max_repeat_tool_calls` loop
  guard now refuses the 3rd identical read first, so the step-end note is the
  loop-guard message instead of "exhausted". **Pre-existing cross-terminal
  drift, not this round**: the verified-success and `completed_steps`
  assertions in that same test pass. Ownership is T01/T04.

## Resume identity closure (2026-09-25)

Runtime `checkpoint.json` now carries a versioned identity tuple containing the
canonical repository path, SHA-256 request identity, repository revision, and
scheduler resume namespace. Workers reject legacy or mismatched checkpoints,
start a fresh attempt, rotate the old model ledger, and emit both a worker
journal event and a unified tracing event. The scheduler supplies a stable
namespace across attempts and a new default namespace for a new scheduler
instance, preventing accidental same-id/shared-root cross-run reuse.

The shared strict-kernel `Checkpoint` contract has the same additive identity
fields. Daily, verified-fix, and legacy-agent strategies validate them before
model/tool execution; the explicit `continue` request is the only compatibility
alias. Arbitrary request changes and repository/revision mismatches are
fail-closed.

Verification after the closure:

- `python -m pytest tests/test_tracing.py tests/test_scheduler_integration.py -q`
  → **79 passed**.
- `python -m pytest tests/test_agent_kernel.py -q -p no:randomly` → **38 passed**.
- `python -m pytest tests/test_agent_sdk.py -q -p no:randomly` → **22 passed**.
- `python -m pytest tests/test_config_trace_state.py -q -p no:randomly` →
  **14 passed**.
- Scoped Ruff for the changed runtime, shared-contract, strict-kernel, and test
  files → **passed**.
- `graphify update .` → **22,355 nodes / 106,333 edges**; the command reported
  2,388 communities and `GRAPH_REPORT.md` reported 2,314.

## Approval identity hardening (2026-09-25)

Approval requests now carry a SHA-256 fingerprint over task id, canonical
repository, issue, and exact diff plus a per-request random id. Decisions are
accepted only for the current request id/fingerprint; mismatched or legacy
requests rotate and invalidate stale decisions. Crash-safe re-entry with the
same identity preserves the request. Focused kernel/tracing/approval coverage
returned 60 passed.

## Terminal 08 bounded orchestration (2026-09-25)

This section is the authoritative handoff for the multi-agent round. The
existing Boundary 3 `harness.core.run_task` and Boundary 6 `Scheduler.run`
paths remain unchanged; orchestration is an additive runtime layer.

### Implemented

- `runtime/roles.py` defines the seven closed profiles: planner/architect,
  implementer, explorer/researcher, reviewer, debugger/tester, verifier, and
  release operator. Each profile has a fixed strategy, visible typed tools,
  default-deny policy rules, mutation/network flags, cost estimate, and
  unverified-completion policy. No profile exposes a task-spawn tool or MCP
  tool, so model recursion is structurally impossible.
- `runtime/worktrees.py` creates detached Git worktrees from one pinned clean
  base commit, captures binary-safe patches, applies dependency patches in
  deterministic order, refuses conflicts, persists worktree records, recovers
  after restart, and refuses dirty cleanup. A dirty source checkout fails
  closed rather than silently omitting local changes.
- `runtime/orchestration.py` owns finite DAG validation, implicit parent-child
  edges, depth/fanout/node/cost/concurrency/retry/wall limits, exact-scope
  claims, pre-spawn approval, durable graph checkpoints, subprocess
  supervision, isolated child sessions, structured handoffs, parent
  continuation runs, event journaling, and live status projections.
- `runtime/orchestration_worker.py` runs one child through the public
  `harness.agent_kernel` API. It restores credentials only from a transient
  scrubbed environment, writes a per-child checkpoint/heartbeat, and never
  receives secrets in its persisted packet. `RunResult` completion semantics
  are preserved instead of laundering planning/review outcomes through
  `TaskResult.success`.
- `runtime/automation.py` provides an idempotent queue, periodic schedule
  registry, and authenticated webhook ingress. It is disabled by default,
  rejects credential-bearing payloads, and only queues immutable workflow
  definitions; it never starts workers.
- `runtime/__init__.py` lazily exports the new entrypoints. The single new
  runtime default is `orchestration_automation_enabled=False` in
  `runtime/config.py`.

### Managed state and recovery

Each workflow owns `logs/orchestrations/{workflow_id}/` with
`orchestration.json`, `events.jsonl`, `claims.json`, `approvals/`, `handoffs/`,
`children/`, and `worktrees/`. Every child gets a unique session and run ID;
parent continuations reuse the parent session with a new run ID. A child crash
or confirmed timeout reuses its run checkpoint within the configured retry
budget. On orchestrator restart, a still-live child PID blocks duplicate
recovery; a dead child is requeued and resumed from its durable checkpoint.

### Verification completed

- Required compatibility command:
  `python -m pytest tests/test_scheduler_integration.py tests/test_ensemble.py tests/test_build_plan.py -q`
  → **72 passed**.
- New regression suite:
  `python -m pytest tests/test_orchestration.py -q -p no:randomly`
  → **19 passed**, including a real Docker-backed implementer/verifier child.
- Required fake stress:
  `python -m runtime.stress --mode fake --tasks 20 --concurrency 20 --kill 4 --out logs/stress/arch08`
  → **all checks passed**, 20/20 successes, four confirmed kill/resumes, max
  overlap 20.
- Scoped Ruff for all new/owned runtime files and the new test → **passed**.
- `git diff --check` → **passed** (only concurrent line-ending warnings), and
  `graphify update .` → **20,863 nodes / 59,966 edges / 4,536 communities**.
- Real Git worktree creation, patch capture/application, dirty-source refusal,
  dirty-cleanup refusal, and clean-worktree removal are covered by the new
  suite. Docker verifier evidence is real, not a fake pass.
- Live-provider lane was not run; provider credentials were not inspected.

### Blocked and cross-owner handoffs

- T4/CLI/SDK/INTERFACES owner: document the additive runtime orchestration
  contract and add an explicit CLI or SDK status/queue surface if desired.
  Do not treat the untracked `agent_sdk/` tree as a stable dependency.
- T2/execution owner: provide a public, lease-protected candidate
  apply/integrate operation if release operator is later expanded beyond its
  implemented review-only profile. This round never claims to publish or
  merge a release.
- A real workflow on this shared checkout is intentionally blocked by its
  pre-existing dirty Git state; clean temporary repositories are used for
  worktree verification. A live provider run remains unverified.
- No marketplace, autonomous swarm, or recursive model spawn path was added.

Machine-readable details and exact integration requests are in
`logs/architecture-round/terminal-08.json`.

## Runtime verification closure rerun (2026-09-24 23:07 IST)

This is the authoritative current-status section. The earlier closeout below is
retained as implementation history; its old full-default-soak blocker is now
resolved by the fresh 3,600-task PASS.

- Hostless runtime core gate: **148 passed, 0 skipped** across scheduler,
  router, difficulty/approval, ensemble, and history-analysis suites.
- Routing: the 11-task ablation set self-check passed 11/11. A fresh paired
  deterministic OFF/ON run completed 6/6 in both arms with 12 calls per arm and
  an ON/OFF proxy-cost ratio of 0.247297. This remains machinery/accounting
  evidence, not live-provider quality evidence.
- Scheduler stress: 20 tasks at concurrency 20 with four confirmed hard kills
  produced 20/20 successes, four preserved resumes, max overlap 20, and an
  empty failure list.
- Real Docker kill/resume: one scripted-model task completed through the real
  harness, Docker sandbox, and verifier; plan reuse, completed-step skip,
  pre-kill trace preservation, and the hard kill/resume all passed.
- Real Docker approval: the current parser-compatible command completed with a
  150-second approval park beyond the 120-second stale window; the decision was
  honored, the killed task resumed, and no gate-parked worker was state-stale
  killed.
- Soak: both CI (**300/300**, 8/8 kill/resumes) and full default (**3600/3600**,
  120/120 kill/resumes, 5.19 aggregate task-hours) profiles passed every check.
  The full run took 800.1 seconds, showed +33.8 MB scheduler RSS growth,
  0.97x last-quarter/first-quarter p95 latency, flat file counts, and zero
  temporary-file residue.
- Abuse: the first invocation's six scenario verdicts passed, but its global
  residue check was contaminated by a separate live Docker workload. After that
  workload exited, a clean rerun passed **6/6** with no `hexec-*` residue.
- Provider matrix remains honestly **blocked**: OpenAI credentials are absent,
  Anthropic is redirected to localhost, and Ollama plus the two required models
  are unavailable. The five pytest skips are not passes.
- The only non-provider release blocker is the existing cross-module decision on
  whether classifier spend belongs in `TaskResult.cost_usd` or remains
  router-ledger-only.
- This verification session changed no production/test code; its durable
  handoff update is this section plus `logs/release-gate/terminal-3.json`.

## Reliability hardening closeout (2026-09-24)

### Scope and current behavior

This pass hardened the scheduler, worker/resume boundary, model-call
ledger, difficulty ingress, offline history analysis, stress/soak evidence,
and adaptive-routing ablation without editing T4-owned shared release files.
The runtime-owned changes are:

- `scheduler.py`, `worker.py`, `checkpoint.py`, `fsutil.py`, `paths.py`:
  unique/validated run and task identities; non-empty resume-directory
  protection; absolute cross-platform path validation; per-attempt process
  launch directories; transient exact-issue/secret transport; redacted
  persistent `task.json`; attempt-scoped heartbeats; two-authority resume;
  coherent checkpoint/result acceptance; confirmed-kill handling; cleanup of
  stale temporary files and run-level worker/temp processes; and fresh-run
  rotation of an old `model_ledger.jsonl` into
  `model_ledger.old-<time_ns>.jsonl` so a same-ID fresh attempt cannot
  inherit old calls.
- `model_router.py`, `difficulty.py`, `mock_provider.py`: context-local
  routing configuration/ledger/usage; concurrent-call isolation; successful
  and failed provider-attempt rows sharing a call ID; redacted failures;
  configured BYO pricing; endpoint fingerprints; recursive secret
  redaction; whole-word routing markers; prompt-scaffolding exclusion after
  `## Retrieved context`; and genuine later verifier/syntax feedback still
  eligible for struggle escalation.
- `analyze_history.py`: canonical lifecycle validation; explicit `fix` or
  legacy mode-less fix traces only; no start-only, conflicting, or
  result/lifecycle-inconsistent records; exact archive filtering; malformed
  local ledger-row tolerance; repository-scoped grouped splits; redaction;
  authoritative module-adjacent calibration output; and apply only after
  actual held-out improvement.
- `ablation.py`, `ensemble.py`: malformed-ledger-row tolerance and endpoint
  identity (provider/model/base fingerprint), so two endpoints using the
  same model name are not compared as equivalent.
- `stress.py`, `soak.py`: kill evidence requires pre-kill progress and
  post-kill confirmation; resume, approval, and lifecycle evidence is
  reconstructed from run journals; incomplete/skipped soak runs report
  `INCOMPLETE`, never `PASS`.

### Verification actually completed

- Initial combined baseline first exceeded the 900-second command cap. The
  isolated split retry passed `95` core tests; the isolated combined retry
  passed `97 passed, 3 skipped` and is preserved at
  `C:\Users\pavan\AppData\Local\Temp\opencode\t3-baseline-combined-retry-20260924\baseline.junit.xml`.
- Final focused command (isolated `HOME`, `USERPROFILE`, `HARNESS_HOME`,
  `HARNESS_DECISIONS_DB`, `HARNESS_LOGS_DIR`, `NEO_CONFIG`, and
  `NEO_TRACE_DIR`; provider credentials removed):
  `python -m pytest -q tests/test_scheduler_integration.py tests/test_model_router.py tests/test_difficulty_approval.py tests/test_ensemble.py tests/test_analyze_history.py tests/test_provider_smoke.py --junitxml=C:\Users\pavan\AppData\Local\Temp\opencode\t3-final-postfix-20260924\focused.junit.xml`
  returned exit `0`: `148 passed, 5 skipped in 95.05s`.
- The five skips are not passes. Exact reasons from
  `python -m pytest -q -rs tests/test_provider_smoke.py` were: one missing
  `OPENAI_API_KEY`, two missing required Ollama models, and two absent or
  placeholder Anthropic credentials. The current machine has no `ollama`
  executable. Historical local-provider passes are not reported as current
  evidence.
- User-authorized alternative live-provider smoke:
  `opencode run --pure --model opencode/space-bunny-free --format json --dir C:\Users\pavan\AppData\Local\Temp\opencode\t3-zen-live-smoke-20260924 "Return exactly NEO_ZEN_SMOKE_OK. Do not use tools."`
  returned exit `0` with exact response `NEO_ZEN_SMOKE_OK`, 8,741 total
  tokens, and `$0` reported cost. The authenticated OpenCode Zen CLI was
  used without reading or copying its stored credential. This validates Zen
  connectivity only; it is not a `runtime.model_router`/litellm integration
  test and does not replace the provider-specific skipped matrix. Artifact:
  `logs/provider/t3-zen-live-smoke-20260924-001/zen_smoke_report.json`.
- Fake stress:
  `python -m runtime.stress --mode fake --tasks 20 --concurrency 20 --kill 4 --out logs/stress/ci-light-t3-20260924-001`
  returned exit `0`, `8/8` checks, `20/20` successes, four confirmed
  kill/resumes, and journal-proven max overlap `20`.
  Artifact: `logs/stress/ci-light-t3-20260924-001/stress_report.json`.
- Real Docker/sandbox/verify stress with a deterministic model:
  `python -m runtime.stress --mode real --tasks 1 --concurrency 1 --kill 1 --out logs/stress/real-kill-resume-t3-20260924-001`
  returned exit `0`, `8/8` checks, `1/1` success, one confirmed kill,
  reused plan, and skipped a completed step on resume.
  Artifact: `logs/stress/real-kill-resume-t3-20260924-001/stress_report.json`.
- Real Docker/sandbox/verify approval stress:
  `python -m runtime.stress --mode real --tasks 1 --concurrency 1 --kill 1 --approval --approval-park-s 150 --hang-stale-s 120 --out logs/stress/real-approval-t3-20260924-001`
  returned exit `0`, `9/9` checks, `1/1` success, one 150-second gate
  park, and zero gate-parked worker kills.
  Artifact: `logs/stress/real-approval-t3-20260924-001/stress_report.json`.
- CI soak:
  `python -m runtime.soak --profile ci --out logs/soak/t3-ci-20260924-002`
  returned exit `0`, `15/15` checks, `300/300` successes, `8/8`
  confirmed kill/resumes, max overlap `25`, and zero final temporary-file
  residue. Artifact: `logs/soak/t3-ci-20260924-002/soak_report.json`.
- Adversarial abuse suite:
  `python -m runtime.abuse --scenario all --out logs/abuse/t3-final-20260924-001`
  returned exit `0`, `6/6` scenarios PASS, and no container residue.
  Retry-loop stopped after 81.4 seconds; attempt-level budget overshoot was
  bounded at 9.36x the tiny cap; adversarial escalation used one hard call
  out of six; the runaway command was killed at its command layer; the
  zero-budget crash terminated after one spawn while the retry case
  disarmed and resumed successfully; and the pre-plan hang was killed with
  terminal timeout after 47.1 seconds. Artifact:
  `logs/abuse/t3-final-20260924-001/abuse_report.json`.
- Offline paired fixture ablation:
  `python C:\Users\pavan\AppData\Local\Temp\opencode\t3_offline_ablation.py logs/ablations/t3-offline-fixture-20260924-001`
  ran both fresh OFF and ON arms through the real scheduler/worker
  subprocesses and returned exit `0`. Each arm completed `6/6` tasks and
  12 calls. Estimated OFF cost was `$0.00185`; ON cost was `$0.0004575`;
  ratio `0.247297`. The driver and report explicitly identify this as
  deterministic fake-model/fake-harness plumbing evidence, not model
  quality or real-provider savings evidence. Artifact:
  `logs/ablations/t3-offline-fixture-20260924-001/offline_ablation_report.json`.
- `scan_tasks(Path("demo/demo-work-agent/logs")) == []`; the concurrent
  demo general-agent run did not leak into fix calibration.
- Final scoped Ruff and `git diff --check` results are recorded in
  `logs/release-gate/terminal-3.json` and its test-result entries.

### Not yet implemented / blocked

- The full default soak command
  `python -m runtime.soak --profile default --out logs/soak/t3-final-20260924-001`
  exceeded the 1,200,000 ms tool cap during batch 0 and wrote no report.
  No soak or worker process remained afterward. The passing 300-task CI
  soak is useful evidence but is not relabeled as the full 3,600-task run.
- The user-authorized OpenCode Zen Space Bunny Free live smoke passed, so
  live model connectivity is available through that CLI. The hard-coded
  OpenAI/Anthropic/Ollama smoke matrix remains blocked because those
  credentials/executable are unavailable; the Zen CLI result is not
  relabeled as a litellm/provider-matrix pass. Real stress runs above use
  scripted deterministic models while still exercising the real harness,
  Docker sandbox, and verifier.
- Per-call budget enforcement remains an attempt-level harness concern. The
  router records every provider attempt and the LLM difficulty-classifier
  call, but the harness `ModelClient` total does not yet include that
  classifier call in `TaskResult.cost_usd`; this needs a T1/T4 contract
  decision rather than a silent runtime approximation.
- A general-agent producer that emits only `task_start` cannot enter
  calibration. The analyzer fails closed; the producer must eventually emit
  a coherent terminal lifecycle event. A scripted task-end must not claim a
  live model source.
- Windows CI should include the atomic-write retry and scheduler integration
  tests. The current focused runtime CI job appears Linux-only.

### Cross-module contract and CI requests for T4

- Reconcile `INTERFACES.md` Boundary 2 with the implemented
  `easy|medium|hard|None` hint set, explicit-hint precedence, predicted
  routing when no hint exists, provider profiles, configured model prices,
  and context-local ledger/usage. The current contract text still describes
  only easy/hard routing inputs.
- Document transient worker secret transport and redaction, per-attempt
  process directories, fresh-ledger rotation, and the two-authority resume
  rules if they are externally observable.
- `cli/main.py:cmd_analyze_history` should catch/format invalid
  `holdout_frac` input; the runtime analyzer correctly raises `ValueError`
  for values outside `(0, 1)`, but the CLI currently leaks that exception.
- Normalize lifecycle documentation around canonical `task_start.mode`;
  terminal `task_end` need not repeat it. Analyzer precedence is
  `task_start.mode`, then an unambiguous legacy mode-less fix trace.
- Add Windows runtime scheduler/atomic-write coverage and decide whether
  classifier-call cost belongs in `TaskResult.cost_usd` or is intentionally
  reported only in the router ledger.
- Do not overwrite the concurrent provider-profile/transport additions now
  present in `runtime/model_router.py` and `tests/test_model_router.py`;
  they were preserved in this closeout and covered by the final focused
  suite.

### Files changed in this closeout

- Runtime: `runtime/ablation.py`, `runtime/analyze_history.py`,
  `runtime/checkpoint.py`, `runtime/difficulty.py`, `runtime/ensemble.py`,
  `runtime/fsutil.py`, `runtime/mock_provider.py`,
  `runtime/model_router.py`, `runtime/paths.py`, `runtime/scheduler.py`,
  `runtime/soak.py`, `runtime/stress.py`, `runtime/worker.py`.
- Tests: `tests/test_analyze_history.py`,
  `tests/test_difficulty_approval.py`, `tests/test_ensemble.py`,
  `tests/test_model_router.py`, `tests/test_scheduler_integration.py`.
- Handoff: `runtime/AGENTS.md` and
  `logs/release-gate/terminal-3.json` (the latter is gitignored).

## Cross-Task Learning round (2026-09-14) — offline history analysis + predictor recalibration loop

An MLOps-style continuous-improvement loop over the accumulated run
history — not per-task retry. New module `runtime/analyze_history.py`
(+ a small opt-in hook in `difficulty.py`); CLI surface
`neo analyze-history`. All data sources are the EXISTING documented
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
- `neo analyze-history --apply` writes the file ONLY when the
  report's held-out before/after actually improved
  (`recommendation == "apply"`); marginal/reject/insufficient →
  nothing written, message says so. Deleting the file reverts to
  built-in bands. Applying is a deliberate human-reviewed step —
  the loop is offline by design (never live/online).

### Task C — the documented maintenance job

`neo analyze-history [--log-root DIR] [--holdout-frac 0.25] [--json]
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
  session finished the steer wiring — `tests/test_cli_neo2.py` +
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
| `analyze_history.py` | **Real (Cross-Task round)** | The offline history-analysis + predictor-recalibration maintenance job: scan accumulated logs (scripted/archive/mode/OFF-arm filters), aggregate predictor divergence / retrieval-vs-repairs / failure patterns, refit score bands on a grouped per-bug train split, honest held-out before/after, gated apply. `neo analyze-history` / `python -m runtime.analyze_history`; report under logs/analyze-history/<ts>/. |
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

## VEX-CEILING-12 - bounded, logged, revertible scheduled runs (2026-09-26)

**NEW 
untime/schedules.py** (sibling of Terminal 08's 
untime/automation.py;
logs/_schedules/ is harness-owned run state, never inside a repository). It
validates and records; it never starts a worker and never imports a provider.
ex run --at <when> "<command line>" is the CLI surface and
python -m runtime.schedules {register,list,claim,complete,revert} is the
operator surface (exit 0 ok, 2 validation error).

### A schedule is a request, not a run

RunSchedule holds a repository, an issue text, a timestamp, and a config
**request**. execution_policy(schedule, inherited=...) is the only thing that
turns it into a Task.config, and it returns `(config, clamped)` where
clamped names every decision the schedule was NOT allowed to make - the
refusal is on the record, not an absence. The rules, in order:

1. credentials in the stored config **or** in the scheduled issue text are
   refused at registration (ScheduleConfigError), so a stored schedule can
   never be a credential store.
2. **pproval is monotone.** MONOTONE_APPROVALS = {auto:0, off:0, never:0,
   require:1}. A schedule may demand 
equire; it may never weaken an
   inherited 
equire to uto. The more restrictive of request/inherited
   wins and the losing request is listed in clamped.
3. every budget key (udget_cap_usd, max_wallclock_s, max_retries,
   max_fetches, max_step_turns) is clamped to the **minimum** of request and
   inherited ceiling, so automation can only ever tighten a run. A note is
   recorded only when the request did not take effect.
4. gent_strategy must be one of the kernel's known strategies; an unknown one
   is dropped with a note rather than guessed at.
5. utomation=True and utomation_schedule_id are written LAST and are not
   overridable, so an unattended run is always identifiable.

**A schedule cannot bypass the verifier either.** complete() stores the run's
own canonical completion status **verbatim** and re-emits it; the registry has
no vocabulary for upgrading completed_unverified. There is no code path in
this module that writes a success status.

### Bounded, logged, revertible

- **Bounded**: --at accepts ISO-8601, a POSIX epoch, or a +15m/+2h/+1d
  offset, and a value **in the past is refused** (a schedule cannot fire
  retroactively because a clock was wrong). A periodic schedule needs an
  interval of at least 60s, at least 1 run, at most 64 config keys, at most 128
  pending schedules, and its schedule_id goes through
  
untime.paths.validate_path_segment (wrapped so callers only ever catch
  ScheduleError).
- **Logged**: every 
egister / due / claim / completed / ailed /
  
everted appends to logs/_schedules/events.jsonl via 
untime.fsutil
  atomic append, redacted. The per-schedule record is an atomic write.
- **Revertible**: 
evert() marks the record, moves it to
  logs/_schedules/reverted/, and writes 
everted_at + 
evert_reason. The
  revert is as auditable as the registration. A reverted schedule is no longer
  claimable.

claim() increments the run counter, advances t for a periodic schedule,
and returns the resolved config the caller MUST use. Consuming the queue is
explicit (claim), never implicit in due() - reading is side-effect free.

### Not implemented, deliberately

No daemon, no OS scheduler registration, no CI-webhook receiver, and no
execution. 
untime/automation.py's authenticated webhook ingress and
WorkflowSpec queue remain the orchestration-side surface; this module is the
single-task ex run --at surface. Wiring a consumer that actually runs a due
schedule is a scheduler-owner handoff recorded in logs/ceiling/terminal-12.json.

### Verification

	ests/test_ceiling12_hooks.py::test_required_4_a_scheduled_run_respects_approval_and_budget_policy
plus the monotonicity, no-upgrade, bounded/logged/revertible, and --at parsing
tests. No Docker, provider, or network lane is required or claimed.
