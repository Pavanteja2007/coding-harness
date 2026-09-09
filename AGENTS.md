# AGENTS.md — Project-Wide Instructions

## What this project is
An AI coding agent harness that fixes real software bugs end-to-end, running on a
custom concurrent runtime with a persistent cross-session/cross-agent memory layer
(exposed via MCP), one measured novel mechanism (adaptive model routing by predicted
difficulty), and a few product-grade touches (git-native output, regression checks,
human-approval mode). Full context and rationale: see `project-spec.md` in this repo —
read it before writing any code.

## This repo is being built by 4 people/agents in parallel, in 4 terminals
Each terminal owns one module. **Read `INTERFACES.md` before writing any code that
crosses a module boundary.** Build against the contracts defined there, using stubs
for modules that don't exist yet. Do not reach into another module's internals — if
you need something from another module that isn't in `INTERFACES.md`, add it there
first (with a Change Log entry) rather than importing private internals directly.

## Locked tech decisions (don't relitigate these mid-build)
- Language: Python throughout
- Model access: via `litellm` for multi-provider support (bring-your-own-key)
- Sandboxing: Docker
- Structural code memory: tree-sitter
- Memory/extensibility protocol: MCP (official Python SDK)
- CLI: a plain Python CLI (argparse or click — Terminal 4's call)
- Benchmark (deferred for now): SWE-bench Lite, when we get to it — not a blocker for early phases

## Conventions
- Type hints on all public functions.
- Every module-level public function needs a docstring stating what it does and what
  it assumes about its inputs (esp. for functions in `INTERFACES.md`).
- Every task run produces a structured log directory at `logs/{task_id}/` — never
  silently swallow a failure; log it.
- Config values (retry limits, budget caps, model choices) go through the `config`
  dict on `Task`, not hardcoded constants — needed for reproducibility (see spec item 10).
- Commit messages: `[module] short description` (e.g. `[harness] add patch validation`).

## When you finish a chunk of work
1. Update `INTERFACES.md`'s Change Log if you changed any contract.
2. Create or update **your own module's `AGENTS.md`** (e.g. `harness/AGENTS.md`)
   summarizing: what's built, what's stubbed/mocked and why, what's left, and any
   decisions future-you or another terminal should know about without re-reading
   all the code. This is how the 4 terminals — and future sessions — stay in sync.
3. Run whatever tests exist for your module before considering a chunk "done."

## Do not skip anything — this is a strict requirement
Every item listed as CORE in `project-spec.md`'s "Full feature inventory" section
belongs to your module, and every one of them must actually be built — not
approximated, not left as a permanent stub, not silently dropped because it
seemed minor or hard. Specifically:
- If something in your terminal's prompt is genuinely ambiguous, ask or make a
  reasonable decision and document it in your module's `AGENTS.md` — do not just
  quietly omit the feature.
- A stub is only acceptable for a dependency owned by *another* terminal (per
  `INTERFACES.md`'s mocking strategy). Never stub out a feature that is your
  own module's responsibility and call it done.
- Before declaring any piece of work finished, go back through the relevant
  numbered items in `project-spec.md`'s feature inventory for your module and
  confirm each one is actually implemented, not just partially addressed.
- If you genuinely cannot implement something (missing tool, blocked by another
  module, etc.), say so explicitly in your module's `AGENTS.md` under a clearly
  labeled "Not yet implemented" section — never let something go quietly missing.
- "Good enough for now" is not an acceptable reason to skip a CORE item. The
  stretch/future-work items in `project-spec.md` are the only things that are
  allowed to be left out entirely.

## Current phase focus (see project-spec.md for full phase plan)
We are starting all 4 modules in parallel using the mocking strategy in
`INTERFACES.md`, each building toward their own Phase 1/2-equivalent milestone
(see each terminal's own prompt for specifics), then integrating.

## What NOT to build right now (see project-spec.md "explicitly out of scope")
No plugin marketplace, no multi-language support beyond Python, no web UI yet
(that's an explicit Phase 6 stretch item), no self-verification/confidence-scoring
features. Stay inside your module's scope from your terminal's prompt.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
