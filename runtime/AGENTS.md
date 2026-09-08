# runtime/ — Terminal 3: Concurrency, Reliability, Model Routing

## Round 2 (2026-09-08) — first real ablation + stress at target scale

### The ablation (Phase 5 first data point) — REAL bugs, REAL models

Runner: `python -m runtime.ablation` (all 5 of Terminal 1's fixture bugs,
real harness + real scheduler workers + real model calls, both arms).

**v1 (honest negative, archived `logs/ablations/v1-heuristic`):** the v1
predictor scored raw message text, but harness prompts are big BY
CONSTRUCTION (system templates + injected file context) — every call
saturated at "hard", so the ON arm degenerated to always-expensive. Also:
the expensive endpoint rate-limits at 8 req/min and 7/10 tasks died with
RateLimitError → the 0% vs 60% "delta" was a rate-limit artifact, not a
mechanism signal. Both findings were fixed before the v2 run (that's what
iteration looks like — v1 was NOT tuned to look good).

**v2 (final, `logs/ablations/v2-heuristic-{off,on-r2}`):**

| arm | success | calls | tokens | cost* | wall |
|---|---|---|---|---|---|
| OFF (always-expensive) | 5/5 100% | 17 | 38,680 | $0.0528 | 575s |
| ON (adaptive v2) | 5/5 100% | 31 | 69,615 | $0.0237 | 300s |

- **Same success rate (100% both arms) at 45% of baseline cost** — the
  spec's expected direction ("similar success rate, much lower cost").
- ON arm model mix: 30 cheap + 1 expensive calls. The one escalation was
  bug01: after failed cheap attempts the predictor's STRUGGLE signal
  (failing-test output in the conversation) escalated 9×easy → 5×medium →
  1×hard and the expensive model finished it. The mechanism did its job
  on a per-task basis, not just on average.
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

- **The resume fix is still NOT landed**: `harness/core.py` has zero
  resume logic; `_fresh_paths` still archives `logs/{task_id}/` wholesale
  on every relaunch. Until T1 implements "resume=True + state.json with
  completed steps → skip those steps instead of archiving", a relaunched
  REAL-harness task restarts from scratch (my scheduler + worker handle
  the runtime side; tested against the fake harness's identical
  contract). The stress results above are with the fake harness (real
  resume path); the real-harness resume path is blocked on T1 only.
- **Your 3 reported scheduler-test failures do not reproduce** (12/12
  pass isolated AND full-suite, on this machine, twice). The full-suite
  failures I see are `tests/test_mcp_server.py::test_stdio_round_trip`
  (only fails in full-suite — order dependence) and
  `tests/test_sandbox.py::TestSandboxIntegration::test_no_container_left_behind`
  (T2's, fails in isolation too). Worth re-running on your side.

## What's built

| Module | Status | Notes |
|---|---|---|
| `model_router.py` | **Real** | Boundary 2 `call_model`, exact INTERFACES.md signature. litellm underneath (pinned `1.74.9` for py3.10). Per-call JSONL ledger + `get_last_usage()`. **NEW: per-tier api_key/api_base, rate-limit backoff, transient-flake retry.** |
| `difficulty.py` | **Real** | **v2**: intrinsic issue signal (first-user-message issue extraction, scaffolding-stripped) + struggle escalation from conversation tail; "llm" estimator w/ heuristic fallback unchanged. |
| `scheduler.py` | **Real** | Process-per-task supervision, concurrency cap (proven at 10-50), FIFO queue, wall-clock + hang timeouts, per-task crash budget with resume-on-relaunch, run-level event journal. **NEW: `live_attempts()` public view; spawn pins resume_dir/log_root (split-brain fix).** |
| `worker.py` | **Real** | `python -m runtime.worker --task-json <path> --run-dir <path>`: loads Task, sets router context + ledger, heartbeats, runs `run_task`, approval gate, writes `result.json` + checkpoint. |
| `approval.py` | **Real** | Cross-process file protocol: worker writes `request.json` and blocks; external approver writes `decision.json`; timeout → failed. Crash-restart-safe. |
| `checkpoint.py` | **Real** | Runtime-owned resume bookkeeping under `logs/{task_id}.runtime/`: `checkpoint.json` (atomic), `heartbeat.json`, `events.jsonl`. |
| `ablation.py` | **Real (new)** | The Phase-5 ablation runner: 5 fixture bugs × on/off arms, real stack end-to-end, per-arm success/cost/token stats from ledgers, honesty notes baked into summary.json. |
| `stress.py` | **Real (new)** | Task B harness: N tasks at target concurrency with simultaneous multi-kill; asserts completion, resume proof, cap proof, journal records; standalone (`python -m runtime.stress`), NOT in the pytest suite (spawns 40-50 real workers). |
| `fake_harness.py` | **Fake (by design)** | Boundary-3-shaped `run_task` with config-driven fault injection (crash/hang/fail-at-step/resume). |
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

`concurrency` (run-level), `max_wallclock_s`*, `crash_retries`, `resume`, `resume_dir`, `approval` ("require"), `approval_timeout_s`, `hang_heartbeat_stale_s`, `adaptive_routing`, `model_tiers` (per-tier `api_key`/`api_base`), `difficulty_estimator` ("heuristic"|"llm"|"off"), `difficulty_llm`, `provider`/`model`/`api_key`/`api_base`, `use_mock_provider`, `mock_responses`, `rate_limit_retries`, `rate_limit_backoff_s`.

*`max_wallclock_s` deliberately matches `harness/config.py` (same key, same meaning) — do not fork its name. `max_retries` stays the harness's verification-retry knob; the scheduler's crash budget is the separate `crash_retries`.

Keys the runtime ADDS to the dict it passes onward (workers see them; harmless if unused): `resume` (bool, resolved by the worker), plus fake-harness test keys when testing.

## litellm version pin — important

`litellm==1.100.0` (latest) **breaks on Python 3.10** (`ImportError: cannot import name 'NotRequired' from 'typing'` inside the Anthropic passthrough path). The environment runs Python 3.10.11. **Pinned `litellm==1.74.9`** — imports and calls cleanly on 3.10. Terminal 1's stub docstring already warns about this; their lazy-import pattern is the right defense and is preserved in the real router.

## What's still fake / pending integration

- **`run_task` real-harness swap is SEMI-AUTOMATIC**: worker `_load_run_task` imports `harness.core.run_task` when available (it now exists!) and passes `log_root` (its optional 2nd param — signature-probed via `inspect`). The fake is pinned only when `use_fake_harness: True` (all scheduler tests use it for determinism). **The blocking gap is on the harness side**: `core._fresh_paths` archives `logs/{task_id}/` on every relaunch, so a resumed task loses state.json and restarts from scratch. Terminal 1 needs to implement the resume contract: when `task.config["resume"]` is True and state.json exists with completed steps, skip those steps instead of archiving + starting over. `harness/context.py` TaskState has the data; only the read-and-skip logic in `run_task` is missing.
- **Real provider smoke**: DONE offline via two real Ollama models (qwen2.5:0.5b, smollm2:360m) through litellm's `ollama/` provider — genuine calls, genuine token counts, full ledger assertions (tests `TestOllamaSmoke`). Cloud tiers (Anthropic/OpenAI) are written and self-skip: this machine's `ANTHROPIC_API_KEY` holds a Groq-format placeholder and `ANTHROPIC_BASE_URL` points at local Ollama; `OPENAI_API_KEY` unset. The smoke tests auto-activate for anyone with valid cloud keys.
- **CLI (Boundary 6)**: Terminal 4 hasn't landed `cli/` yet; `runtime.scheduler.run(...)` is ready for them to call.

## Test coverage (all green as of this pass)

`tests/test_model_router.py` (22), `tests/test_difficulty_approval.py` (8), `tests/test_scheduler_integration.py` (12 — real process kills, hang detection, approval file protocol, ablation toggle), `tests/test_provider_smoke.py` (Ollama 2 passed; cloud 3 self-skip). Full-suite run: **222 passed, 3 skipped, 0 failed** (includes all other terminals' tests). Stress runs (`python -m runtime.stress`) are separate from pytest by design.

Definition-of-done checks, verified not assumed:
- ✅ Scheduler runs 10+ concurrent fake tasks; concurrency cap proven from event journal (`max_overlap <= cap`); parallel beats serial floor.
- ✅ Mid-task crash (hard `os._exit`) → resume from completed steps, proven by: 2 worker starts (fresh, resume), state.json all-steps-complete, attempt=2, event journal (`crash`→`crash_retry`→`finish`).
- ✅ Hang → killed via stale state.json + one-shot injection disarm → completes after resume.
- ✅ Adaptive routing toggle demonstrably changes model choice + ledger cost (ablation architecture test).

## Known limitations / decisions future-you should know

1. **Windows process semantics**: `proc.kill()` on Windows is hard-kill ( TerminateProcess) — workers get no cleanup chance. That's exactly the crash scenario we checkpoint for, so it's fine (and the fake's `os._exit` matches).
2. **Hang detection granularity**: state.json mtime staleness requires the harness to touch state.json per step; if the real harness goes minutes between state writes (e.g. one long test run), raise `hang_heartbeat_stale_s` accordingly or add a step-level progress file.
3. **PID reuse** (fsutil `is_pid_alive`): heuristic only; never used for correctness decisions.
4. **Approval gate file protocol** assumes one approver; concurrent conflicting decisions resolve last-write-wins via atomic replace.
5. **The ablation IS run** (this round): v1 honest negative + v2 positive result archived under `logs/ablations/` with proxy-price honesty notes. Next step for Phase 6: bigger task set, repetitions, real paid tiers.
6. **`logs/` is gitignored** — all run state (ablations, stress) lives there; nothing in-repo depends on committed artifacts. Summary stats are recorded HERE and in each run's `summary.json`/`stress_report.json`.
7. **Cheap-tier endpoint variance**: nararouter (qwen3.8-27b) went from 12/12 instant replies to 90-240s/call and one hard-down window within a single evening; the router's transient retry is what saved the v2 ON arm. If a future ablation behaves erratically, check endpoint health first (the probe scripts pattern is in the Round-2 story above).
