# Vex — AI Coding Agent Harness: Complete Project Knowledge Base

> **Purpose of this file:** This is a complete, self-contained description of the
> entire project — every layer, every feature, how it is built, and *why* each
> design decision was made. It is written so that an LLM (or a person) can read
> ONLY this file and generate resume bullet points (Google XYZ format:
> "Accomplished [X] as measured by [Y], by doing [Z]"), interview talking points,
> or a portfolio write-up.
>
> **Framing note:** This document describes the system as designed and intended
> at completion (all CORE spec items built and integrated). It deliberately
> contains NO numeric figures/metrics — bullets generated from it should use
> qualitative outcomes or leave [Y] as a placeholder for real measured numbers
> to be filled in from the final benchmark runs.

---

## 1. The one-paragraph pitch

Vex is an AI coding agent harness that **autonomously fixes real software bugs
end-to-end**: given a bug report and an unfamiliar repository, it retrieves the
relevant code, plans a multi-step fix, edits the code inside a hardened Docker
sandbox, runs the test suite, classifies failures, retries with adaptive
feedback, and only declares success when a verifier (real tests, never the
model's own claim) passes — then emits a git branch, commit, PR description,
and a human-readable rationale. It runs on a **custom concurrent runtime**
(process-per-task scheduling, checkpoint/resume, cost-aware model routing) with a
**persistent cross-session, cross-agent memory layer** exposed as an **MCP
(Model Context Protocol) server** — so Claude Code, Cursor, or any MCP client
can query the same structural code graph and learned decision memory. The
measured novel mechanism is **adaptive model routing by predicted subtask
difficulty** (cheap model by default, expensive escalation only when difficulty
or live struggle signals justify it), validated via paired on/off ablations
through the real end-to-end stack. The entire system was built by 4 parallel
agents/terminals against strict interface contracts (INTERFACES.md), with
adversarial security testing, fault-injection stress testing, cross-platform
CI, and a prompt-regression eval harness.

**The differentiator (say it this way in interviews):** harnesses, runtimes,
and code-memory MCP servers each exist separately (SWE-agent, Aider,
codebase-memory). Vex's contribution is the **integration**: one system where
memory feeds the router's difficulty prediction, the runtime's checkpointing
preserves work across crashes, the harness's verifier gate eliminates false
successes, and everything is reusable by external agents via MCP.

---

## 2. System architecture — the four layers

```
                    ┌────────────────────────────────────────┐
   CLI (vex)  ─────►│  RUNTIME (Terminal 3)                 │
   interactive,     │  scheduler · worker subprocesses      │
   benchmark,       │  checkpoint/resume · hang detection    │
   status, dashboard│  model router (adaptive difficulty)    │
                    └───────────────┬────────────────────────┘
                                    │ one worker subprocess per task
                    ┌───────────────▼────────────────────────┐
                    │  HARNESS (Terminal 1) — the agent loop │
                    │  retrieval · planner · step sessions    │
                    │  editor · verifier gate · repair loop  │
                    │  context management (state/reset/RECALL)│
                    └──────┬─────────────────────┬───────────┘
                           │                     │
          ┌────────────────▼──────┐   ┌──────────▼──────────────┐
          │ EXECUTION (Terminal 2)│   │ MEMORY + MCP (Terminal 4)│
          │ Docker sandbox        │   │ tree-sitter code graph   │
          │ verifier (flake-aware)│   │ SQLite decision store    │
          │ git output · rationale│  │ MCP server + MCP client  │
          └───────────────────────┘   └─────────────────────────┘
```

- **Language/stack:** Python throughout; litellm for multi-provider LLM access
  (bring-your-own-key); Docker for sandboxing; tree-sitter for structural code
  memory; official MCP Python SDK; SQLite (WAL) for decision memory; rich for
  the CLI UX; argparse CLI + library-first core (the CLI is a thin wrapper —
  the runtime spawns harness instances programmatically).
- **Cross-module contracts:** shared dataclasses (`Task`, `TaskResult`,
  `ExecutionResult`, `VerificationResult`) in `shared/types.py`; every boundary
  documented in INTERFACES.md with a change log; modules never reach into each
  other's internals — everything crosses through documented signatures.
- **The whole system is also a library:** `harness.core.run_task(task) ->
  TaskResult` is the single most important contract — the scheduler's entire
  job is calling it many times concurrently with checkpointing around it.

---

## 3. Layer 1 — The Harness (the agent loop that fixes one bug)

### 3.1 The core loop (how a bug gets fixed)

Given `Task{task_id, repo_path, issue_text, config}`:

1. **Snapshot** — the agent NEVER touches the original repo. Every run copies
   the repo into `logs/{task_id}/pristine/` + `work/`; all diffs are computed
   between those; rollback restores from pristine. (The original repo being
   byte-identical after a run is a tested invariant.)
2. **Baseline verify** — run the target test on the PRISTINE copy first. If it
   already passes, the task is mislabeled and short-circuits to success with
   zero model calls (prevents burning budget on non-bugs). This also captures
   the pre-fix test output that feeds failure feedback later.
3. **Retrieval** — find the relevant files WITHOUT dumping the whole repo into
   context (see 3.3).
4. **Memory-informed planning** — query the decision store for past decisions
   recorded against THIS repo (conventions, past mistakes, known gotchas) and
   inject them as a planner-prompt section. The planner produces 2–4 small,
   independently verifiable sub-steps, each with a "done when" checkpoint —
   deliberate task decomposition to avoid "one-shotting" (a documented LLM
   failure mode).
5. **Attempt loop** — each step is a FRESH bash-session with the model: it
   issues one command per turn (read/edit/test), with a `SUBMIT` control signal
   ending the step. Failed steps feed structured feedback (verifier output
   tails, tool-error classifications) into the next attempt.
6. **Verifier-gated completion** — `status="success"` ONLY when the real
   verifier confirms: target test passes AND full suite regression passes AND
   the result is not flaky. The model claiming "done" NEVER completes a task.
   A model that SUBMITs without fixing → task FAILS (tested).
7. **Product output on verified success** — git branch + commit + PR
   description + rationale.md (see 3.7).

### 3.2 Verifier-gated completion (the philosophical core)

- The harness never trusts the model's own "it's done" claim — only sandboxed
  test results decide. This eliminates the classic agent failure mode of
  self-declared success on broken code.
- **Multi-layer verification:** baseline (pre-fix) pass to catch mislabeled
  tasks; flaky-test detection (same test rerun multiple times; differing
  outcomes across reruns — including timeout as a distinct third outcome —
  flags flaky, so a task never "passes" on a flaky green); full-suite
  regression check so fixing the target test doesn't silently break
  everything else.
- **Adversarial prompt-injection defense:** if issue text contains "ignore
  previous instructions" style attacks ordering the agent to defuse the test
  (e.g., rewrite it to `assert True`), the harness's edit-policy gate
  (protected paths, re-validation before final verify) poisons the attempt —
  the verifier never re-litigates policy. This closes a real success-path
  bypass class (found by adversarial testing, fixed, regression-tested).

### 3.3 Context retrieval — two-layer strategy

- **Layer 1 (grep):** keyword/glob search over the issue text's terms.
- **Layer 2 (structural):** consumes the memory layer's tree-sitter code graph.
  The key mechanism is the **target-test anchor**: the failing test's file →
  its imports → the module under test; its calls → the symbols it exercises.
  This finds the right files even when the issue text names nothing. Plus
  **subword symbol matching** (`compute_monthly_total` decomposes into
  {compute, monthly, total}, so an issue saying "monthly total is doubled"
  matches without the identifier appearing verbatim) and one-hop call-graph
  neighborhood expansion (callers/callees of matched symbols).
- Structural hits outrank grep hits in the merge. The whole thing is
  best-effort: graph unavailable → grep-only, never crashes.
- **Why:** solves the real context-window problem — inject only what's relevant
  to the current sub-step, never the whole repo.

### 3.4 Context management (grounded in documented LLM failure modes)

- **Structured state file, not chat history:** `state.json` (plan,
  completed_steps, files_touched, decisions, remaining_plan) is the source of
  truth, atomically rewritten each step — context resets at every step boundary
  with non-lossy structural handoff, so long tasks don't decay.
- **RECALL protocol (reversible compaction):** a step session can output
  `RECALL <terms>` instead of a bash command; the harness greps the task's own
  full-fidelity trace log and re-injects matching entries into the live
  session. Old detail is compacted out of active context but always retrievable
  on demand — never destroyed. Budgeted per step so it can't deadlock.
- **Constraint re-injection:** issue one-liner, current step, remaining steps,
  protected paths, "don't modify tests" appended to the END of every tool
  result — because instruction compliance measurably decays with context
  distance.
- **Curated per-step context:** each step gets only its step's files + retrieval
  hits, capped — not a static growing bundle.
- **BATCH protocol:** multiple independent read-only commands (strict verb
  allowlist, no composition characters, all-or-nothing validation) executed
  concurrently in one turn — measured to roughly halve read-phase wall-clock
  vs serial commands, and collapse multiple model round-trips into one.
- **DOCS protocol:** `DOCS <target>` resolves library/API documentation from a
  shared cache → pydoc subprocess → opt-in PyPI metadata, read-only by
  construction, network off by default.

### 3.5 Agent execution layer & tooling

- **Bash-only action space** (deliberate, mini-swe-agent-proven): one command
  per turn, output-capped, fenced-block and heredoc-safe extraction, deny-pattern
  guard blocking destructive commands (reordered `rm -fr` flags, pipes from
  curl/wget into shells, fork bombs) while benign look-alikes stay allowed.
- **Structured tool errors:** every failing tool result is classified into a
  stable error kind (file_not_found, command_not_found, malformed_patch,
  syntax_error, import_error, undefined_name, permission_denied, timeout,
  argument_error, command_rejected) with a suggested fix — the model gets an
  actionable signal instead of raw stderr noise.
- **Lint/static-analysis gate:** stdlib AST pass (syntax + module-level
  undefined names, false-negative-biased by design so it never blocks a
  verifiably-correct fix) at two points: in-session at SUBMIT time, and as a
  pre-final-verify short-circuit so a known-broken edit gets retried WITHOUT
  burning a full sandboxed pytest cycle. Never gates success — the verifier
  stays absolute.
- **Agent-written tests:** the agent can be configured to write its own
  edge-case tests beyond the given suite (self-verification), again
  verifier-gated.

### 3.6 Repair loop & coordinated multi-file changes

- **Feedback-driven repair:** failed attempts roll back the working copy (or
  the declared change group) and retry with structured feedback: verifier raw
  output tails, classified tool errors, lint findings flow step-to-step and
  attempt-to-attempt.
- **Coordinated-change detection (atomic multi-file fixes):** before planning,
  the structural graph is walked — every CALLER of a symbol defined in a
  changed file plus every IMPORTER of a changed module becomes a dependent
  file set; the planner declares atomic `change_group`s. A pre-verify gate
  poisons any attempt that changes some-but-not-all members of a group (the
  verifier is never asked to re-litigate group completeness — a suite without
  call-site coverage would happily pass a half-updated rename), and rollback is
  atomic across the whole group. This is what makes multi-file renames/API
  changes land correctly instead of as half-updated breakage.
- **Protected-path guard:** tests/, .git/.hg/.svn, config-globbed paths —
  normalized against traversal tricks (`..` normalization, forged VCS paths
  detected in a separate work/-scan), so the agent cannot edit tests to make
  them pass, or forge version-control state.

### 3.7 Product-grade outputs

- **Git-native output:** on a verified fix, the private work copy gets a real
  `git init` → pristine-first commit (via `git --work-tree` so the fix commit's
  `git show` diff IS exactly the fix) → fix commit on a `harness/fix-<slug>`
  branch, with a meaningful commit message and a full PR description
  (problem / changes / diff / verification / rationale). Per-invocation author
  identity (never writes global git config), user hooks disabled, original
  repo untouched.
- **Rationale log:** a deterministic, no-model-call, human-readable paragraph
  per task — what was wrong (parsed from the baseline pytest failure), what
  changed (files from state.json), why (decisions), verdict (from the final
  result) — written on both successes AND failures (a rationale is valuable on
  losses too; an unverified diff never gets git output).
- **Human-in-the-loop approval mode:** optional mode where, after verification,
  the worker parks in an approval gate (request.json with the diff → external
  approver → decision.json). Approve → fix applied + git output; reject →
  downgraded to failed, diff stripped. The scheduler's hang detection exempts
  gate-parked workers (heartbeat still beats) while wall-clock caps still
  bound unbounded parks. Plus a plan-preview mode: render the first plan
  (numbered steps + checkpoints) and gate the EDIT phase before it starts.

### 3.8 Reproducibility & config discipline

- Every tunable (retry caps, budget caps, wall-clock, model pins, protected
  paths, feature toggles) flows through `task.config` merged over documented
  defaults — no hardcoded constants; every run's full config (minus api_key)
  is recorded in its trace for exact reproduction.
- **Full traceability:** every prompt, model response, tool call, tool result,
  verify, decision, recall, and routing event lands in an append-only
  `trace.jsonl` per task that survives crash-resumes as first-class history.

---

## 4. Layer 2 — The Runtime (concurrency, reliability, routing)

### 4.1 Concurrent scheduler

- **Process-per-task worker pool:** the scheduler spawns one `python -m
  runtime.worker` subprocess per task, FIFO queue, concurrency cap enforced by
  construction (spawn-gated) — designed for 10–50 concurrent agents and proven
  under real Docker load with mid-run kills. Workers are stateless relative to
  each other; the design is horizontally scalable (external queue, no shared
  in-process state).
- **Two-authority checkpoint design:** the harness's state.json says WHAT to
  skip (completed steps); the runtime's checkpoint.json says WHETHER to resume
  (crash budget, in-flight status). This separation keeps each module's
  authority clean.

### 4.2 Checkpoint/resume — crash-tolerant by design

- A crashed or hard-killed agent's task is requeued and RESUMED, not restarted:
  persisted plan reused (no re-planning), completed steps skipped, partial
  edits in the work copy built upon, in-flight attempt continued (a crash is
  an interruption, not a verification failure — no retry budget consumed),
  pre-crash spend seeded into the budget cap, trace appended across
  relaunches.
- Validated by real fault injection: kill agents mid-run (hard `proc.kill`,
  `os._exit`), verify every killed task resumes and finishes; crash budgets
  exhausted → terminal error status, never a lost task; a deliberate
  crash-loop breaker disarms re-queued identical faults (a perpetual
  same-crash respawn is impossible by construction).
- **Hang detection:** heartbeat daemon per worker (heartbeat.json), state
  staleness check, wall-clock backstop — three interlocking liveness signals,
  tuned to real measured work-phase gaps (a sandboxed pytest run legitimately
  takes longer than a naive heartbeat window; the gate-parked-worker exemption
  prevents killing a task paused for human approval).

### 4.3 Model/provider abstraction (the routing plumbing)

- litellm underneath → any provider, bring-your-own-key, per-call
  model/provider/api_key overrides; tier table where each tier can carry its
  own endpoint and key (multi-gateway). Per-call JSONL ledger records model,
  tier, tokens, cost, routed-via — the ablation's data source and the
  dashboard's model-mix data.
- Resilience: provider 429s back off exponentially with bounded retries;
  transient upstream errors (5xx, connection resets, empty-message gateway
  flakes) get short retries; authentication errors deliberately NOT retried
  (fail loud); optional per-call completion-token budget caps runaway
  reasoning-token burn.

### 4.4 The novel mechanism — adaptive model routing by predicted difficulty

**What it is:** per-call (and per-task) difficulty prediction deciding which
model tier handles each step: easy/medium → cheap fast tier, hard → expensive
tier, with a live **struggle signal** (failing verifies, repeated errors in
the conversation tail) escalating mid-task when the cheap tier demonstrably
can't finish.

**How the predictor works (v2 design):**
- **Intrinsic signal** from the ISSUE TEXT ONLY (the planner prompt's issue
  section, stripped of scaffolding — because prompts are padded by
  construction and a naive raw-text scorer saturates at "hard" for everything;
  this was the honest v1 negative result that drove the redesign).
- **Struggle signal** from the conversation tail (recent failures, error
  classes).
- Hint precedence: explicit model pin > difficulty hint > defaults — callers
  can always force a model.

**Multi-candidate ensemble extension (separately ablated mode):** for
hard-predicted tasks, run TWO full cheap-tier candidate fixes in parallel and
escalate to one expensive run only if BOTH miss — structural insurance against
both cheap-tier capability ceilings and expensive-tier latency death, at the
cost of the second candidate.

**Why it's defensible:** SWE-bench's own team documented that even RANDOM
per-turn model switching outperforms either model alone — smart routing is a
measured upgrade on a documented real effect. Validated via paired on/off
ablation arms through the REAL end-to-end stack (real scheduler, real
subprocess workers, real harness loop, real Docker-sandboxed pytest, real
model calls, per-call ledgers), reproduced across multiple independent runs
and task sets (fixtures, synthesized bug classes, and real unfamiliar OSS
repos), with honest negative results kept in the write-up (the v1 predictor
saturation; ensemble cost-neutrality at narrow tier price ratios).

### 4.5 Adversarial cost/resource-abuse hardening (every cap proven to FIRE)

- Retry-loop attack (endpoint returning 429 forever) → bounded backoff
  exhausts, task errors, never unbounded sleeping.
- Budget overshoot → cap fires at attempt granularity (documented honest
  granularity limit: cap + one attempt's calls).
- Engineered "scary text" difficulty-inflation attack → bounded false
  escalation (at most the planner call escalates; fix steps stay cheap).
- Runaway commands → layered kills (sandbox per-command timeout kills the
  container; wall-clock cap kills the task).
- Hung model calls (endpoint that accepts and never responds) → state-stale
  hang detection kills and requeues; wall-clock backstop.
- Crash-loop injection → budget exhaustion → terminal error, never lost, never
  infinitely respawning.

---

## 5. Layer 3 — Execution (sandboxing & verification)

### 5.1 Hardened Docker sandbox

- **Fresh `--rm` container per command**: repo bind-mounted read-write at
  /workspace (agent edits persist to the host work copy — the harness diffs
  host-side), everything else hardened: `--network none` by default (network is
  opt-in per call), `--read-only` rootfs + tmpfs /tmp, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, memory/CPU/PID limits, non-root user,
  `--pull=never`.
- **Never silently unsandboxed:** Docker down → loud `SandboxUnavailableError`
  → task status "error"; no fallback to running uncontained (a deliberate
  fail-loud decision).
- **Dependency-image lifecycle:** per-repo images built lazily from dependency
  manifests only (requirements.txt / pyproject [project] deps), fingerprinted
  so CODE EDITS NEVER TRIGGER REBUILDS; the repo's own package is never
  pip-installed (installed copies would shadow the agent's bind-mounted edits —
  tests always exercise the source being fixed).
- **Concurrency hardening (found under real 40–50-task load):** container names
  embed the owner host PID; every sandbox call runs a rate-limited opportunistic
  sweep reaping containers whose owner process died (surviving peers clean up
  after hard-killed workers within seconds — no orchestrator needed);
  cross-process image-build race solved with an O_EXCL lockfile with
  dead-holder stealing.
- **Adversarially proven containment:** 24/24 sequential attack classes held
  (host mounts, filesystem escape, pid-namespace, docker-socket access, su
  escalation, workspace-sibling traversal, network egress, env leakage; fork
  bombs collapsed at pids-limit; memory bombs OOM-killed; tmpfs floods ENOSPC'd
  at exactly the cap; CPU pinched under --cpus; infinite spins timeout-killed;
  output floods bounded) plus concurrent hostile bursts at 16-wide concurrency
  with clean canary tasks.

### 5.2 The verifier (stateless, flake-aware)

- `verify()` is a stateless evaluator of ONE repo state: target test rerun
  N times (flake = >1 distinct outcome label, where TIMEOUT is a distinct
  label — a pass-then-hang reads as flaky, never as a stable pass), then the
  full suite for regression. Caller picks pristine vs edited (baseline
  division of labor: verify cannot reconstruct pristine state itself).
- Per-repo test-command pinning (addopts overrides, env passthrough,
  deselection with documented reasons) so unfamiliar OSS suites run clean in
  slim containers.

### 5.3 Git output & rationale generation (deterministic, host-side)

- Pure host-side functions, no Docker/model/network: branch + two-commit
  structure (pristine root commit so the fix commit's diff IS the fix),
  collision-suffixed branch names, never touches the user's repo or global
  git config; rationale built deterministically from the trace + state file.

---

## 6. Layer 4 — Memory + MCP (persistent, cross-session, cross-agent)

### 6.1 Structural code graph (tree-sitter)

- Indexes a repo's functions/classes/methods/modules/files (with file:line +
  docstring first lines) and calls/imports/defines edges; persisted per-repo
  index with mtime-based reuse (code edits → full rebuild is fine at this
  scale; lazy rebuild makes pruning safe).
- Query verbs: `symbol X`, `callers X`, `callees X`, `importers M`,
  `imports M`, `file`, `files`, `symbols` — structural questions ("what calls
  run_task?") answered without re-reading files.
- Documented honest over-approximation: name-based call resolution (right
  recall-over-precision trade for a read model — never presented as
  type-accurate).

### 6.2 Decision/pattern memory (SQLite, WAL)

- A running store of what the system LEARNED: why a library was chosen, known
  gotchas, per-repo conventions, past mistakes and the strategies that fixed
  them. Ranked keyword search (match count, then recency), repo-scoped
  (normalized path comparison so relative/absolute forms of the same repo
  match), thread-safe, idempotent ingestion (unique index on task_id+text →
  re-scanning a grown/rewritten state file is a no-op), recursive poll at any
  logs-tree depth + optional background watch thread.
- **The closed loop:** harness decisions → state.json → auto-ingested →
  queried by the NEXT task's planner (repo-scoped, injected as a
  "Relevant past decisions" prompt section placed so it can never shift
  difficulty scoring) → agent avoids re-tripping known mistakes. Validated by
  a real-model paired ablation (measured: known-mistake recurrences
  eliminated and model calls/tokens/cost reduced at equal success —
  efficiency + mistake-avoidance, honestly NOT added task-solving power on
  easy sets).
- **Why this layer exists:** switching agents/models/conversations means
  starting from zero and re-burning tokens re-understanding the project.
  Querying memory for exactly what's relevant is the fix — and exposing it
  over MCP means ANY tool benefits, not just this harness.

### 6.3 MCP server + client (extensibility both directions)

- **Server (expose):** standalone stdio MCP server (official SDK, 2.x with
  1.x fallback) exposing five tools: `query_structure` (code graph),
  `query_decisions` (decision memory), `record_decision`, `task_status`
  (harness state), `list_repos`. Any MCP client (Claude Code, Cursor) can
  connect and query the same memory — cross-session by construction (disk-
  backed), cross-agent by construction (any process shares the store).
- **Client (consume):** the project can equally spawn and call EXTERNAL MCP
  servers over stdio (`vex mcp list-tools` / `vex mcp call`) — consuming the
  wider MCP ecosystem, not just publishing to it.
- Cross-platform SDK pitfalls found and fixed (Windows errlog import-time
  binding poisoned under pytest capture; explicit errlog passed at the one
  spawn site — documented for anyone spawning MCP stdio servers in tests).

---

## 7. Interfaces — CLI, library, MCP, dashboard

### 7.1 The `vex` CLI (primary human interface)

- `vex fix --repo --issue` (single task, direct library call);
  `vex run-benchmark --subset --concurrency` (through the real scheduler);
  `vex status --task-id` (renders state.json + enriches from the trace);
  `vex memory record|query-decisions|query-structure|ingest`;
  `vex dashboard`; `vex mcp list-tools|call`; exit-code contract 0/1/2.
- **Interactive natural-language mode (primary UX):** bare `vex` on a TTY →
  a plain-language REPL: a typed sentence IS the issue text, repo inferred
  from CWD, mid-session repo/model switching, live status spinner with
  per-event labels and an accruing cost ticker, colored diff rendering,
  markdown rationale, interactive approval prompts, live benchmark table,
  graceful Ctrl+C (checkpoints kept, this process's orphaned containers
  swept).
- **Session persistence:** `--continue` / `--resume <id>` / `--list-sessions`
  — an append-only session index + directory-scan fallback discovers
  resumable runs (completed AND remaining steps AND no final result);
  resume rebuilds the Task from the run's own task_start trace event. Live-
  verified by hard-killing a run mid-step and continuing it to completion.
- **Slash commands** (/status /diff /sessions /resume /approve /reject
  /cancel /quiet /help) and a TOML config file (~/.vex/config.toml) with
  explicit-flag > file > defaults precedence, broken-TOML-tolerant.
- **Cross-platform polish:** encoding-probed glyph fallbacks (legacy cp1252
  consoles never crash on the first emoji), piped output has no ANSI,
  packaging fixed for real `pip install -e .` on Windows/Linux/macOS.

### 7.2 Read-only web dashboard

- GET-only stdlib HTTP server (loopback, background refresh thread,
  atomically-swapped snapshots, no build tooling) over the structured logs
  the system already writes — task status colors, per-task cost/model-mix/
  difficulty-hint distribution, plan progress, aggregate pass/fail/cost
  cards, run grouping. No new backend, no database, no write path — a thin
  visualization layer by design. Performance-tuned at production log-tree
  scale (pruned directory walk + tail-chunked trace reads instead of
  full-file rglob/reads).

---

## 8. Observability & evaluation infrastructure

### 8.1 Unified cross-module tracing

- ONE append-only normalized event stream per task that every layer (runtime
  scheduler/worker, model router, sandbox, MCP/memory) emits into — so a
  task's full lifecycle (planning, tool calls, verifies, routing decisions,
  memory queries, sandbox commands) is reconstructible from ONE place instead
  of half a dozen files across three trees. Never-raise contract (tracing is
  observability, not correctness — a tracing failure can never change a
  task's outcome), opt-in via env, zero-overhead no-op when unset.
- `python -m shared.traceview <task_id>` merges the unified stream with the
  harness trace, the worker journal, and the routing ledger into one
  chronological timeline + summary.

### 8.2 The prompt-regression eval harness

- A fixed 12-task × 6-arm eval matrix that answers one question before any
  prompt change ships: **did it break anything that used to work?** Each arm
  disables one improvement feature (memory-informed planning, lint gate, DOCS
  lookup, agent tests) or the whole round, against the current baseline,
  through the REAL loop (real harness, real Docker sandbox/verifier,
  deterministic scripted model). A REGRESSION (any task or loop-integrity
  check that worsens vs baseline) fails CI with exit code 2. Includes
  scenario tasks that REQUIRE the repair/feedback loop to carry (attempt 1
  lands a syntax error, attempt 2 must fix it).
- **Why this matters:** non-deterministic systems need automated regression
  gates for prompt changes the same way code needs tests — this is CI for
  prompt engineering, and it found real bugs in its own task set
  (multi-command script replies being silently beheaded; CRLF line endings
  false-passing host checks while false-failing in the Linux sandbox;
  shared-repo pollution from prior runs).

### 8.3 Benchmark discipline (SWE-bench Lite, the headline proof)

- Standardized evaluation on SWE-bench Lite (300 real GitHub bug-fix tasks),
  reported pass rate WITH and WITHOUT the novel mechanism (real before/after
  ablation, never one naked number), iterated on small subsets under explicit
  dollar budgets before full runs. Cheap/open model as default tier,
  frontier model only as the escalation the routing decides on.
- Multi-repo generalization testing beyond the benchmark: cloned real
  unfamiliar OSS repos at pinned SHAs with genuine introduced bugs encoded as
  failing regression tests, each run through the FULL real stack with
  behavioral validation of the fix in-sandbox, original repo untouched.
- Honest-results culture: every ablation run's summary carries its caveats
  (proxy pricing, sample size, endpoint variance, invalid-run annotations);
  a consolidated RESULTS.md keeps every number traceable to on-disk ledgers;
  negative results are kept and explained, not buried.

---

## 9. Engineering practices (the "how it was built" story)

- **Parallel multi-agent development with contracts:** the system was built by
  4 simultaneous agents/terminals, each owning one module, building against
  documented interface contracts (INTERFACES.md) with a change log and
  stub-first/mock-first strategy (each terminal builds against exact-signature
  stubs for not-yet-built neighbors; real modules auto-resolve when they land).
  Contract discipline: additive schema changes only, unknown-key passthrough,
  fail-loud defaults.
- **Verification-first culture:** every module's DoD is proven by real
  end-to-end runs (real Docker, real subprocesses, real fault injection),
  not unit tests alone; "verified, not assumed" is the recurring standard —
  claims are checked against on-disk evidence, and audits correct the record
  when numbers were misattributed.
- **Adversarial security testing as a first-class round:** deliberate attack
  matrices against every surface — sandbox escape (24 attack classes),
  prompt injection (found and fixed a real success-path bypass),
  path traversal via task ids (found and fixed a real data-leak: pathlib's
  `/` discards the base for drive-lettered operands on Windows — fixed with a
  shared semantic guard, not a charset allowlist), SQL injection, secret
  harvesting, shell-injection payloads, cost-abuse triggers — each pinned by
  permanent regression tests replaying the actual exploit over the real
  transport/subprocess.
- **Fault-injection stress + soak testing:** standalone harnesses spawn real
  40–50-way concurrent workers with mid-run hard-kills and verify resume,
  leak-freedom (zero container/child residue, bounded image growth), and a
  long-duration soak (thousands of sequential tasks) checking memory
  stability (RSS growth bounded and attributed via tracemalloc), latency
  drift, artifact flatness — the soak found and fixed a real
  Windows atomic-replace race (concurrent reader → transient PermissionError
  → bounded retry).
- **Cross-platform CI:** GitHub Actions matrices (Linux/macOS/Windows ×
  Python 3.10/3.12) with Docker-availability-aware test selection (clean
  skips with reasons on Docker-less cells), nightly full-scale stress jobs,
  lint ratchets (one canonical linter; new code violation-free; baseline
  debt may only shrink), per-module CI jobs with warm-image strategies.
- **One canonical dev setup:** idempotent one-command setup script, pre-commit
  hooks, structured error reporting (plain-language cause + check-lines;
  raw tracebacks saved to files, never displayed).

---

## 10. Technology stack summary

| Concern | Choice |
|---|---|
| Language | Python (3.10 floor, 3.12 CI) |
| LLM access | litellm (multi-provider, BYO key, per-call routing) |
| Sandboxing | Docker (hardened flags, per-repo dep images) |
| Structural memory | tree-sitter |
| Decision memory | SQLite (WAL), ranked keyword search |
| Extensibility protocol | MCP (official Python SDK, stdio, server AND client) |
| CLI | argparse + rich (interactive mode, themed output) |
| Dashboard | stdlib HTTP, read-only, log-scanning |
| Tracing | append-only JSONL per task, cross-module overlay |
| CI | GitHub Actions matrix + nightly stress + lint ratchet |
| Testing | pytest, scripted deterministic models, Docker-gated e2e, fault injection, adversarial suites, soak |

---

## 11. Bullet-generation guide (for the LLM reading this)

**What to emphasize (highest-signal, resume-grade):**
1. End-to-end autonomy with **verifier-gated completion** (never trusts the
   model's self-assessment — eliminates false successes).
2. **Adaptive model routing by predicted difficulty** — the measured novel
   mechanism, ablated with/without through the real stack (cost reduction at
   equal success is the headline; fill [Y] with final SWE-bench numbers).
3. **Docker-sandboxed execution, adversarially proven** (24 attack classes,
   prompt-injection bypass found+fixed, fail-loud no-silent-unsandboxed).
4. **Crash-tolerant concurrent runtime** (checkpoint/resume validated by
   fault injection; hang detection with approval-gate exemptions; anti-
   crash-loop by construction).
5. **Persistent cross-session/cross-agent memory exposed as an MCP server**
   (closed loop: decisions → store → next task's planner; queryable by
   Claude Code/Cursor).
6. **Context engineering** (structured state file, per-step resets, RECALL
   reversible compaction, constraint re-injection, BATCH concurrent reads,
   target-test-anchored structural retrieval).
7. **Product-grade outputs** (git branch/commit/PR + rationale; human-in-the-
   loop approval; plan preview; interactive CLI with live cost ticker).
8. **Evaluation discipline** (prompt-regression eval harness as a CI gate;
   ablation methodology with honest negative results; SWE-bench Lite with
   before/after reporting).
9. **Security hardening** (path traversal data leak found+fixed with a shared
   semantic guard; SQL injection containment; secret harvesting sweeps;
   cost-abuse caps proven to fire).
10. **Multi-agent build process itself** (4 parallel agents, contract-first
    development, additive schema evolution — a systems-engineering story).

**Rules for generating bullets:**
- Use the completed-system framing (everything above is built or designed-to-
  completion; describe it in present tense as shipped).
- Google XYZ: "Accomplished [X] as measured by [Y], by doing [Z]" — since this
  file deliberately omits figures, either use qualitative [Y] ("at equal
  task success", "under deliberate fault injection", "across N attack
  classes" → replace N) or leave a placeholder for the final benchmark
  numbers.
- Every bullet should name the MECHANISM (the Z): e.g., "by classifying tool
  failures into stable error kinds with suggested fixes", "by embedding the
  owner PID in container names so surviving peers reap orphans", "by
  rerunning the target test with timeout as a distinct outcome so
  pass-then-hang reads as flaky".
- Distinguish measured claims (ablation-backed) from capability claims
  (built-and-tested) — never invent numbers that aren't in this file.
- Interview one-liner for the project overall: *"I built an AI agent that
  fixes real bugs in real repos end-to-end — it plans, edits in a hardened
  sandbox, verifies with real tests, repairs from feedback, routes each step
  between cheap and expensive models based on predicted difficulty, survives
  crashes mid-task via checkpoint/resume, remembers what it learned across
  sessions, and exposes that memory to other tools over MCP."*

---

## 12. Scope boundaries (future-work only — deliberately NOT core)

Plugin marketplace; multi-language support beyond Python; web UI beyond the
read-only dashboard; self-verification confidence scoring; interactive
clarification on ambiguous issues; multi-repo memory sharing. These are
documented future work, not built and not claimed.
