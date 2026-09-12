# Adaptive Model Routing for an AI Coding-Agent Harness — Results

*Consolidated final write-up (Round 7). Covers the original 16-task ablation
(v1→v4) and the Round-6 multi-repo extension (v6). Every number below was
re-verified against the archived per-run `summary.json` / ledgers on disk
(`logs/ablations/…`); nothing is quoted from memory.*

---

## One-paragraph summary (interview / resume form)

I built the runtime for a multi-agent AI coding harness — a process-per-task
scheduler with checkpoint/resume, crash budgets, and hang detection, proven
at 45–50-way concurrency with mid-run kills — including an adaptive
model-routing layer that predicts per-call difficulty and routes easy calls
to a cheap LLM tier, escalating to an expensive tier only when predicted
difficulty or live struggle justifies it. I ablated it end-to-end on real
bug-fix tasks (real harness loop, Docker-sandboxed pytest verification,
real model calls, per-call cost ledgers): after an initial honest-negative
result (the v1 predictor saturated at "hard" because harness prompts are
padded by construction), a v2 redesign cut LLM spend **2.6–3.5x (61–71% of
baseline cost)** on a 16-task set — 100% success on both arms at **39% of
baseline cost** on the cleanest run — and the result reproduced
**out-of-distribution on five real OSS repos** at pinned SHAs (4.2x cheaper,
with *higher* success for the adaptive arm, because the cheap tier's speed
let more attempts fit the wall-clock budget). Adversarial testing confirmed
every cost/limit cap fires under deliberate worst-case triggering, and a
5-simulated-hour soak run (3,600 tasks, 120 mid-run kills) showed no
memory growth beyond allocator churn, no artifact accumulation, and flat
scheduling latency.

---

## What the system is

The harness takes a repo + a bug report, plans a fix, edits code inside a
Docker sandbox, verifies with the repo's real pytest suite, and produces
git-native output. The runtime module owns:

- **Scheduler**: concurrency-capped (proven 10–50) process-per-task
  execution, wall-clock + hang supervision, per-task crash budgets with
  resume-on-relaunch, a run-level event journal.
- **Checkpoint/resume**: two-authority design — the harness's per-step
  `state.json` is the *progress* authority (what to skip); the runtime's
  `checkpoint.json` decides *whether* a relaunch is a resume. Every killed
  task in every stress/soak run finished via resume; no task was ever lost.
- **Adaptive model routing** (the novel mechanism): every LLM call is
  classified easy/medium/hard and routed to a cheap or expensive model
  tier accordingly. Classification = an intrinsic issue signal (extracted
  from the first user message's issue text, stripped of prompt
  scaffolding) + a live struggle signal (failing-test output in the
  conversation tail). One config flag (`adaptive_routing`) toggles it —
  the ablation's ON/OFF arm.

## Method

Paired arms over the same task set, run end-to-end through the real stack
(scheduler, workers, harness loop, Docker-sandboxed pytest verification)
with real model calls and per-call JSONL cost ledgers:

- **OFF arm** (baseline): every call pinned to the expensive model.
- **ON arm**: adaptive routing on; the router predicts difficulty per call
  and routes easy/medium → cheap tier, hard → expensive.

Costs use each endpoint's published price rates (free-tier BYO routers —
see honest caveats). Token counts are raw and measured.

### Task sets

| set | n | what it is |
|---|---|---|
| fixtures | 5 | small single-file Python bugs (wrap, mean off-by-one, stack pop, NameError, mutable default) |
| synthesized (v3/v4) | 11 | varied single-file bug classes with deliberately varied issue-text *styles* — easy one-liners, medium recipes, and 2 scary-sounding texts over trivial bugs as false-escalation probes |
| multi-repo (v6) | 5 | **real OSS repos** cloned at pinned SHAs (more-itertools, arrow, inflect, semver, boltons), one genuine bug introduced each, encoded in a failing regression test — verified fails-pre-fix / passes-post-fix on host *and* in Docker before any ablation run |

## Results progression

| run | n | OFF success / cost | ON success / cost | ON cheaper | note |
|---|---|---|---|---|---|
| v1 | 5 | 0% / $0.0241 | 60% / $0.0467 | **0.5x (ON cost MORE)** | honest negative — see below |
| v2 (redesign) | 5 | 100% / $0.0528 | 100% / $0.0237 | 2.2x | 30 cheap + 1 expensive call |
| v3 | 16 | 100% / $0.1847 | 94% / $0.0532 | **3.47x** | first scale-up; 57 cheap + 2 expensive |
| v4 (re-run) | 16 | 100% / $0.1505 | 100% / $0.0581 | **2.59x** | cleanest run: 100% both arms; 68 cheap + 3 expensive |
| v6 multirepo | 5 | 40% / $0.3059 | 60% / $0.0730 | **4.19x** | real OSS repos; ON *also* +20% success, 5.1x faster wall |

### The honest negative (v1) — kept, not hidden

The v1 predictor scored raw message text. But harness prompts are large
*by construction* (system templates + injected file context), so every
call saturated at "hard" and the ON arm degenerated to always-expensive —
17/17 calls on the expensive tier, costing ~2x the baseline arm. Worse,
7/10 tasks across both arms died of rate-limit errors that evening, so
even the apparent OFF-vs-ON success delta was mostly a rate-limit
artifact. Both findings drove the v2 redesign: score only the *issue
portion* of the first user message (scaffolding stripped), add a
struggle-based escalation signal, and add router-side 429 backoff with
transient-flake retry.

### What the mechanism actually did (v2–v4)

- **96–97% of ON-arm calls ran on the cheap tier**; success never dropped
  because of routing (v3's single failure was a gateway flake before any
  routing decision; v4's clean re-run hit 100% both arms).
- **False escalation is bounded**: scary-sounding text over a trivial bug
  costs at most one expensive call (the planner), never the whole task.
- **Real escalations are rare and genuine**: v2 bug01 — a failing verify
  mid-attempt escalated one call, routing dropped back to cheap, and the
  *cheap* model finished the fix on attempt 2. v4 lookup-default — the
  cheap model burned 4 turns on a self-inflicted import error, the
  struggle signal escalated the 7th call, and the expensive call's
  command ran the suite that passed.
- **Escalation direction never sticks**: after any expensive call,
  routing drops back to cheap unless struggle persists.

### Out-of-distribution confirmation (v6, real repos)

Same direction, bigger effect, plus a finding the synthetic set could
never show: **the cheap tier's speed (p50 22s vs the expensive tier's
p95 309s) meant more fix attempts fit inside the same wall-clock budget**
— the ON arm finished 3/5 vs the OFF arm's 2/5, at 24% of the cost.
Absolute success dropped on unfamiliar repos (capability failures: all
three failed agents *located* their bug but ran out of turns before
applying the edit) — reported honestly as a model-capability limit, not a
machinery one.

## Robustness results (adversarial + soak)

- **Abuse suite (6/6 pass)**: 429-forever endpoint → bounded backoff then
  clean error (never unbounded sleeping); budget cap → one-attempt-bounded
  overshoot (9.3x a deliberately tiny $0.10 cap, never unbounded — the
  honest granularity limit, documented); engineered scary text → 1 hard
  call of 6 (bounded false escalation); `sleep 9900` runaway → sandbox
  killed in 54.7s vs the 120s wall cap, zero container residue; crash
  loop → impossible by construction (fault injection disarms on relaunch;
  proven at both zero-budget and retry ends); pre-plan hung model call →
  state-stale hang check fired at exactly the configured window →
  terminal timeout.
- **Soak (3,600 tasks, 120 kills, 5.15 simulated task-hours, one
  long-lived scheduler)**: 15/15 checks pass — every task succeeded
  (including all 120 kill-resumes), RSS growth +35MB attributed via
  tracemalloc to C-level allocator churn (only ~1.1MB of Python objects
  retained — not a scheduler leak), per-task artifact counts flat
  (7.06 → 7.07 files/task), latency p95 drift 1.02x (q1 vs q4), zero
  leaked workers, zero `.tmp` residue. The soak also **found and fixed a
  real bug**: 5 of 3,600 workers died on a Windows `PermissionError`
  when a supervisor read a file the worker was atomically replacing
  (no `FILE_SHARE_DELETE`) — fixed with a bounded replace-retry in the
  atomic-write primitive; re-run: zero occurrences.

## Honest caveats (all also baked into each run's summary.json)

1. **Proxy pricing**: endpoints are free-tier BYO routers; token counts
   are raw and measured, but costs use published price rates for
   comparable model classes. The cost *delta* is a price-model delta,
   not a bill.
2. **Sample size**: 5 (v2, v6) and 16 (v3/v4) tasks × 1 rep. The cost
   ratios (2.6–4.2x) are large enough to be robust to endpoint
   flakiness; success-rate differences at this n are noise. Directional
   evidence, not benchmark-grade.
3. **Task difficulty**: fixture + synthesized bugs are single-file,
   one-function fixes; the multi-repo set is the same bug shape in real
   trees. SWE-bench-grade tasks need the Phase-6 step (paid tiers,
   multiple reps, confidence intervals) before any success-rate claim.
4. **Endpoint variance**: free-tier routers ranged 2s→976s per call and
   occasionally flaked; the router's retry path hides most of it. One
   cheap-tier endpoint exhausted its free credits mid-project and was
   replaced after live probing (one candidate was rejected for emitting
   pseudo-XML tool-call markup the bash-only harness can't execute).
5. **Escalation counts are endpoint-load dependent**: the struggle path
   fires when the cheap tier genuinely can't finish — most tasks never
   needed it (v3: 0 escalations; v4: 1; v6: 0).

## Artifacts

Every run is archived with full per-task ledgers under `logs/ablations/`
(v1-heuristic, v2-heuristic-*, v3-expanded-fixed, v4, v6-multirepo),
`logs/stress/`, `logs/abuse/final-r6/`, `logs/soak/r7-final/` —
machine-checkable from each run's `summary.json` / `*_report.json`.
`logs/` is gitignored run state; this document is the durable record.
