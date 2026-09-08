# Project Spec: AI Coding Agent Harness with Adaptive Runtime & Persistent Memory

## One-line summary
An AI system that automatically fixes real software bugs — reads an issue, understands the codebase, writes a fix, tests it, and repairs itself if it fails — running on a custom concurrent runtime with a persistent cross-session memory layer, one measured novel mechanism, and a handful of product-grade touches that make it feel like a real tool, not a benchmark script.

## Background / how this project was chosen (for context in the new chat)
- Purpose: portfolio project for SWE + AI/ML job applications, competing against strong candidates (including IIT-tier) for roles at large companies.
- Existing projects: ServoPilot (concurrent Python/Qt robotics control, real verified metrics — systems-heavy but not AI), Self-Improving Coding Agent (DPO fine-tuning + ChromaDB retrieval — more of a distillation/research project than a real coding agent), RLHF from Scratch (reward modeling + active-learning annotation platform — some metrics currently unverified/assumed, needs fixing before it goes near an interview). None of the three combine real AI decision-making with real product-grade systems engineering — that's the gap this project fills.
- The search process ruled out, in order: generic agent/RAG/fine-tuning ideas (too common), pure ML-research directions (interpretability, distributed training, custom CUDA kernels — genuinely novel but conflicts with "not much deep ML" preference and huge execution risk), ever-bigger abstractions (multi-agent orchestrator → generic scheduler → "compiler for agent workflows" — correctly flagged as scope that risks ending up half-finished).
- Landed on: a coding agent harness + a lightweight runtime underneath it + one properly-measured novel mechanism, plus a persistent memory layer — scoped to actually finish in ~6 months with real rigor, rather than an ever-expanding architecture diagram.
- Key reframe on "originality": individually, agent harnesses, task runtimes, and codebase-memory MCP servers all already exist as separate tools (SWE-agent, mini-swe-agent, Aider, OpenCode; codebase-memory, agentmemory, codevira, etc.). The differentiator is **integration** — one system where the memory layer feeds the router's difficulty prediction, the runtime's checkpointing preserves memory state across crashes, and the whole thing is usable by external MCP clients. Say it this way in interviews, not "nobody's built this."
- Timeline: ~6 months, intentionally — the time is meant to go into rigor and real benchmark numbers at each phase, not into piling on more architectural concepts.

## The core layers

### 1. The Harness — the "brain and hands" that fixes one bug
Given a bug report and a codebase, it:
- Retrieves the relevant part of the repo without dumping the whole thing into context
- Plans what needs to change
- Edits the code (patch or full-file rewrite)
- Runs the test suite in a sandbox
- If it fails, diagnoses *why* (syntax error vs logic error vs wrong file vs flaky test) and adapts the next attempt instead of blindly retrying
- Never self-declares "done" — completion is always gated by the verifier/test result

Similar in spirit to SWE-agent / mini-swe-agent / Aider; mini-swe-agent (a ~100-line bash-only harness scoring >74% on SWE-bench Verified) is a useful proof that harness simplicity plus a good model beats over-engineering.

### 2. The Runtime — the operations layer underneath the harness
- Runs multiple harness instances concurrently (target: 10-50 agents at once)
- Checkpoints progress so a crashed/slow agent resumes without losing work
- Routes each step to a cheap or expensive model based on cost/difficulty (cost-aware routing)
- Model/provider abstraction layer: bring-your-own-API-key, multi-provider (via e.g. litellm), so provider/model choice is a config value per call, not hardcoded — this is also what makes the adaptive-routing mechanism possible
- Draws directly on prior experience: ServoPilot's thread-safe dispatch/fault handling, and the coding agent's multi-key API failover

### 3. The Project Memory Layer — persistent, cross-session, cross-agent
Addresses a real, personally-experienced pain point: switching agents/models/conversations means starting from zero and burning tokens re-understanding the project, which also fills the context window. Note: this exact problem already has an active ecosystem of solutions (codebase-memory, agentmemory, codevira, live-memory, CogmemAi — some with real traction, e.g. 38k+ GitHub stars) — so the goal here is a well-integrated version, not a claim of inventing the concept.
- **Structural memory**: a code knowledge graph (e.g. tree-sitter based) — functions, classes, imports, call relationships — so structural questions don't require re-reading files.
- **Decision/pattern memory**: a running store of facts the harness has learned (why a library was chosen, known gotchas, conventions, past bugs), updated as it works.
- Exposed as **your own MCP server** — meaning it isn't locked to your harness; any MCP-compatible tool (Claude Code, Cursor, etc.) could query it too. This is also the direct fix for the token-waste/context-window problem: query memory for exactly what's relevant instead of re-reading everything.

### 4. The Novel Mechanism — pick ONE, prove it with an ablation
- **(a) Adaptive model routing**: predict per-subtask difficulty, route easy steps to a cheap/fast model, hard steps to an expensive one. Prove via ablation: similar success rate, much lower cost, vs. always using the expensive model. (Real supporting evidence: SWE-bench team found even *random* switching between two models per turn outperformed either model alone — smart routing is a defensible upgrade on a documented real effect.)
- **(b) Smart failure repair**: classify *why* a fix attempt failed (wrong file, syntax error, flaky test, logic error) and respond with a strategy specific to that failure type. Prove via ablation: higher success rate vs. naive blind retry.
- Whichever is chosen, this is what makes the project "yours" rather than a harness clone. Budget real iteration time — the first version usually doesn't show a clean win, and that's normal, not a sign of failure.

### 5. Context management (folds into the harness + memory layer)
Grounded in real, documented failure modes (Anthropic's own engineering team calls long-running agent coherence "an open problem"):
- **Structured state file** (JSON/markdown), not raw chat history, as the source of truth for what's done/planned/remaining — rewritten each step, not accumulated.
- **Deliberate context resets** at logical checkpoints, with structured (not lossy-summarized) handoff.
- **Task decomposition** into small, independently verifiable sub-steps — avoids "one-shotting" (trying to do too much in one pass, a documented common failure).
- **Constraint re-injection** near the end of context — instruction compliance measurably decays with distance/turns (one study: 73% compliance at turn 5 down to 33% at turn 16 with no instruction change), so critical constraints get restated near the model's most recent context, not assumed remembered from early on.
- **Verifier-gated completion** — never trust the model's own "it's done" claim.

### 6. Extensibility via MCP
- Consumes external MCP tools/connectors where useful
- Exposes the harness's own capabilities (memory queries, task status) as MCP tools, so it's usable by other agents/tools, not a closed system

## Product-grade extras (small additional cost, real added value — add these)
1. **Git-native output**: proper branch + meaningful commit messages + PR description explaining the fix and why — not just a raw diff. Makes demos feel like a real tool.
2. **Regression check**: after the target test passes, run the broader test suite to confirm nothing else broke — cheap to add given sandboxed execution already exists.
3. **Human-in-the-loop approval mode**: optional mode where the agent proposes a fix and pauses for approval before applying it to a real repo — a genuine trust-building capability, not just a toggle.
4. **Rationale/explanation log per fix**: a human-readable paragraph per task — what was wrong, what changed, why — built from the existing debug logs.

## Explicitly out of scope for now (future-work only, do not build as core deliverables)
Full plugin/skill marketplace, multi-language support beyond Python, self-verification via agent-written edge-case tests, interactive clarification, confidence-aware output, multi-repo memory sharing. These are real ideas but each is its own mini-project; adding them now risks the "half-built but impressive on paper" trap this project was deliberately scoped to avoid.

## The proof (benchmark) — deferred, revisit later
- Standard: **SWE-bench Lite** (300 real GitHub bug-fix tasks) — accepted standard for this scale of project; do not attempt full SWE-bench (research-lab scale).
- Report pass rate **with and without** the novel mechanism (real before/after, not one number).
- Resume bullet target: *"Built an AI coding agent harness with a custom concurrent runtime, persistent memory layer, and [adaptive routing / smart repair], achieving X% on SWE-bench Lite — Y% better than the naive baseline."*
- Cost reality check: cost varies roughly 300x across models for comparable/lower accuracy in published evaluations (e.g. $4.72 for one model vs. $1,600+ for another on the same 50-task subset). Plan: cheap/open model as default tier, frontier model only as the escalation tier your routing mechanism decides on; iterate on small subsets (e.g. SWE-bench Verified Mini, 50 tasks), save full 300-task runs for final numbers; set an explicit dollar budget before running full evaluations.

## Scale targets
- Concurrency: 10-50 concurrent agents (not thousands) — enough to prove the mechanisms work under real contention.
- Infra: single machine or 2-3 small cloud VMs; architecture designed to look horizontally scalable (stateless workers, external queue) even if validated at small node count.

## Phase plan (6 phases, each independently shippable — safety net built in)
**Phase 1 — Bare single-agent harness (MVP).** One agent fixes one bug on a small repo. No sandboxing/concurrency yet. Stack: Python, one LLM provider, direct file read/write + git diff, pytest via subprocess. Steps: repo loader (dumb retrieval) → planner prompt → editor → test runner → loop controller (~3 retry cap) → logging. Done when: fixes a real majority of 3-5 varied hand-picked bugs, with full logs.

**Phase 2 — Real context management + sandboxed execution.** Proper retrieval strategy for large repos (can reuse ChromaDB experience). Docker sandboxing so it's safe on arbitrary real repos. Done when: runs safely on an unfamiliar real repo.

**Phase 3 — Repair loop + first benchmark numbers.** Real failure-diagnosis-and-retry logic. Get SWE-bench Lite environments running (notoriously fiddly) and get a first pass-rate number on a 30-50 task subset. Done when: a legitimate, if modest, benchmark number exists.

**Phase 4 — The runtime layer.** Concurrent execution, checkpoint/resume, basic cost-aware routing. Real fault-injection testing (kill an agent mid-task, confirm correct resume), not just "ran once." Done when: 10-50 agents run concurrently and reliably, including recovering from induced failures.

**Phase 5 — The novel mechanism + ablation.** Build the chosen mechanism properly. Run the controlled with/without comparison. Expect iteration. Done when: a genuine, defensible ablation result exists (positive or an honest, understood negative).

**Phase 6 — Full benchmark run + polish.** Full 300-task run for headline numbers. Small results dashboard/write-up. Clean repo, README, technical report. Done when: a finished, presentable project with real numbers front and center.

**Where the memory layer (MCP) and product-grade extras fit:** build the memory layer starting around Phase 2-3 (once there's real repo interaction to feed it) and refine through Phase 4-6; the git-native output, regression check, approval mode, and rationale log are best added in Phase 6 polish, since they're cheap additions on top of already-working pieces, not blockers for earlier phases.

**Safety net:** Phases 1-3 alone already produce a legitimate, working, benchmarked harness. Phases 4-6 elevate it to the differentiated, higher-tier project. If time runs short, stopping after Phase 3 still leaves something real to show.

## Open decisions to make when starting the build
- Which novel mechanism: (a) adaptive routing or (b) smart repair classification?
- Target practice repo/bugs for Phase 1 (small, real-ish, genuinely varied)
- LLM provider(s) to start with
- Exact cost budget for the full project

## Full feature inventory (every feature discussed, explicitly — nothing folded away silently)

**Harness core mechanics (all CORE — required for the harness to function at all):**
1. Repo understanding / context retrieval (start dumb — grep/mentioned files — improve over phases)
2. Tool interface / action space (bash-only vs. structured read/search/edit/test tools — decide early)
3. Patch/edit generation + validation (diff or full-file rewrite; validate a patch applies cleanly before testing)
4. Sandboxed execution environment (Docker; resource limits; no network unless needed)
5. Verification beyond "tests pass" — run a baseline (pre-fix) test pass to confirm what was already passing, and detect flaky tests (same test, different outcomes across runs)
6. State/memory *within* a single task — track what's already been tried so the agent doesn't repeat failed attempts
7. Stopping conditions — max retries, max cost, max wall-clock time per task
8. Logging / full traceability — every prompt, response, tool call, result, per task
9. Model/provider abstraction layer — bring-your-own-key, multi-provider (e.g. via litellm), per-call model selection (required for the novel routing mechanism)
10. Config / reproducibility — every run's settings (model, retries, repo/commit, prompt version) recorded alongside logs

**Context management (CORE — grounded in documented real failure modes):**
11. Persistent structured state file (JSON/markdown) as source of truth, not raw chat history
12. Deliberate context resets at logical checkpoints with structured (non-lossy) handoff
13. Reversible compaction — drop old detail from active context but keep it retrievable on demand, never destroy it
14. Task decomposition into small, independently verifiable sub-steps (avoids "one-shotting")
15. Constraint re-injection near the end of context (compliance measurably decays with distance/turns)
16. Curated per-step context injection — re-retrieve only what's relevant to the current sub-step, not a static growing bundle
17. Verifier-gated completion — the harness's test/verification layer decides "done," never the model's own claim

**Runtime layer (CORE):**
18. Concurrent execution of multiple harness instances (target 10-50 agents)
19. Checkpoint/resume so a crashed or slow agent doesn't lose progress
20. Cost-aware routing infrastructure (the plumbing the novel mechanism runs on)

**Project memory layer (CORE):**
21. Structural code graph (tree-sitter based) — functions, classes, imports, call relationships
22. Decision/pattern memory — architecture decisions, conventions, past bugs, learned over time
23. Exposed as an MCP server — persists across sessions *and* agents, queryable by external MCP clients

**The one novel mechanism (CORE — pick ONE):**
24. (a) Adaptive model routing by predicted subtask difficulty, validated via ablation, OR
25. (b) Failure-classifying repair strategy vs. naive retry, validated via ablation

**Product-grade extras (CORE — cheap, high perceived value):**
26. Git-native output — branch, commit messages, PR description
27. Regression check — run full test suite after the target test passes, not just the one test
28. Human-in-the-loop approval mode — optional pause-for-approval before applying a fix
29. Rationale/explanation log — human-readable paragraph per fix (what, why)

**Extensibility (CORE, lightweight):**
30. Consume external MCP tools/connectors where useful
31. Expose the harness's own memory/status as MCP tools for other agents to use

**Explicitly STRETCH / future-work only (discussed, deliberately deferred, not dropped):**
32. Multi-file, cross-cutting coordinated changes across several files at once
33. Self-verification — agent writes its own additional edge-case tests beyond the given suite
34. Cost/quality transparency dashboard per task (which model handled which step, and why)
35. Confidence-aware output (agent reports how confident it is and why, not just pass/fail)
36. Interactive clarification when an issue description is ambiguous
37. Rollback/undo with clean diff history
38. Multi-attempt strategy diversity beyond the chosen repair mechanism (e.g. minimal-patch vs. broader-refactor attempts)
39. Language-agnostic support beyond Python
40. Web UI/CLI live dashboard for watching a run
41. Full plugin/skill marketplace
42. Multi-repo memory sharing across unrelated projects

Rationale for the stretch list: each is a real, legitimate idea raised during scoping, but adding all of them as core deliverables is what turns a finishable 6-month project into a perpetually-expanding one — the exact trap this project was scoped to avoid. Treat #32-42 as an explicit "future work" section in the final write-up, which is itself a legitimate and common thing to include, not a concession.

## Interface
- **CLI** — primary, human-facing interface (`harness fix --repo ... --issue ...`, `harness run-benchmark`, `harness status`). Matches every serious reference tool in this space (SWE-agent, mini-swe-agent, Aider) and is fastest to build.
- **Library** — core harness logic built as a callable Python library first; the CLI is a thin wrapper around it. Required regardless of interface choice, because the runtime (Phase 4) needs to spin up 10-50 harness instances programmatically, not by shelling out to CLI commands.
- **MCP** — a separate interface, not accessed via the CLI: the project-memory server is queried over the MCP protocol by whatever client connects (your own harness, or external tools like Claude Code/Cursor). This is a second, distinct consumer of the system, not extra scope.
- **Web UI — stretch only, built last (Phase 6 polish), and kept intentionally minimal.** Rationale: the runtime's differentiators (concurrent agents, live cost/routing, checkpointing) are hard to convey through terminal logs alone, and a simple screenshot/GIF of a live dashboard is far more persuasive to a recruiter skimming a README than log output — real demo value, low cost. Keep it cheap by design: a **read-only** dashboard that visualizes data the system is already logging (task status, per-agent cost/model, pass/fail counts) — not a new backend, just a thin visualization layer on existing structured logs. Do not let UI polish compete for time with the routing/repair mechanism, the ablation, or the memory layer — those carry the actual technical weight in interviews.

## Known risk to manage honestly
The novel mechanism (Phase 5) is where projects like this succeed or fail on rigor — first attempts often don't show a clean effect. Budget real iteration time for it, not just as a final add-on step. Also: don't let scope creep back in past what's listed here — this document represents the outcome of an extended back-and-forth that deliberately settled on this scope as the finishable, defensible version.
