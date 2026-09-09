# coding-harness

An AI coding agent harness that fixes real software bugs end-to-end:
one system where a Docker-sandboxed agent loop, a concurrent
checkpointing runtime, a persistent cross-agent memory layer (exposed
via MCP), and **adaptive model routing by predicted difficulty** are
integrated deliberately — the integration is the point, not any one
piece.

## What it does

```
harness fix --repo <path> --issue "<bug report>"      # one bug, one agent
harness run-benchmark --subset tasks.json             # N bugs, N supervised agents
harness status --task-id <id>                         # structured progress view
harness dashboard                                     # read-only web view of a run
harness memory query-decisions "<topic>"               # the persistent memory layer
harness mcp call "<server cmd>" <tool> [--args '{..}'] # consume any external MCP server
```

Under the hood, per task: the repo is snapshotted (the original is
never touched), a planner decomposes the fix into small verifiable
steps, an agent executes them with bash inside a locked-down Docker
sandbox (read-only rootfs, no network, resource limits, capability
drop), and **completion is verifier-gated** — a task only reports
`success` when the target test passes AND the full suite shows no
regressions. On verified fixes the harness additionally writes
git-native output (branch + commit + PR description), a human-readable
rationale.md, and a structured trace of every prompt/response/tool
call.

## The four layers

| Layer | What it is | Where |
|---|---|---|
| **Harness** | planner / step agent / verifier gate, repo snapshot + diff, git-native output, rationale log, resume contract | `harness/` |
| **Execution** | Docker sandbox (fresh container per command, orphan reaping, serialized image builds), stateless verify + flake detection | `execution/` |
| **Runtime** | process-per-task scheduler (proven at 10–50 concurrent), checkpoint/resume across hard kills, approval gate, adaptive model router + per-call cost ledger | `runtime/` |
| **Memory + MCP** | tree-sitter code graph, SQLite decision memory, MCP server exposing 5 tools to any MCP client (Claude Code, Cursor, …), MCP client for consuming external servers | `memory/`, `mcp_server/` |

## The novel mechanism: adaptive model routing

Per call, the runtime predicts difficulty (intrinsic signal from the
issue text + struggle signal from the conversation tail — failing test
output, burned turns) and routes easy/medium calls to a cheap model,
hard calls to an expensive one. Every call lands in a per-task JSONL
ledger (model, tokens, cost, hint) — the mechanism is measurable, not
asserted.

**Ablation results (real bugs, real models, full real stack —
scheduler subprocesses → real harness → Docker verify):**

| run | arm | success | calls | tokens | cost* | wall |
|---|---|---|---|---|---|---|
| 5 fixture bugs | always-expensive | 5/5 | 17 | 38,680 | $0.0528 | 575s |
| 5 fixture bugs | adaptive | 5/5 | 31 | 69,615 | $0.0237 | 300s |
| 16-task expanded set | always-expensive | 16/16 | 81 | 138,526 | $0.1505 | 2717s |
| 16-task expanded set | adaptive | 16/16 | 71 | 136,436 | $0.0581 | 812s |

(Aritfacts: `logs/ablations/v2-heuristic-*` (n=5) and
`logs/ablations/v4` (n=16; an earlier `v3-expanded` run is invalid —
fixture-path bug — superseded by `v3-expanded-fixed` and `v4`.)

- Same 100% success rate in every arm/run, at **45% (n=5) / 39%
  (n=16) of baseline cost** — same direction, growing margin with a
  more varied task set (16 tasks: 5 real fixture bugs + 11 synthesized
  repos with varied bug classes AND varied issue-text styles).
- Escalations did what they should: one genuine struggle escalation
  (cheap attempts failed → hard-tier call finished the task), zero
  cost-wasting escalations across the 8 easy-styled texts, and 1 of 2
  deliberately-SCARY texts (stack trace + "race" wording over a
  one-token bug) tricked the intrinsic scorer into one expensive call —
  the honest false-escalation data point (1/16 tasks).

\* Honesty notes: endpoints are free-tier BYO routers; token counts and
model-choice data are raw measurements from ledgers, costs use proxy
price rates for comparable model classes (both endpoints report no
cost) — the cost **delta** is a price-model delta, not a bill. n=5 and
n=16 × 1 rep are directional, not benchmark-grade. Phase 6 should
re-run on SWE-bench subsets with paid tiers.
## Runtime reliability (proven, not claimed)

- **45 tasks @ concurrency 45, 8 simultaneous mid-run hard kills**:
  45/45 success, 8/8 genuine resumes — verified from trace events
  (`plan_reused`, `step_skipped_resume`, pre-kill trace survival), zero
  leaked containers (`logs/stress/real-45/`).
- Concurrency cap proven from the event journal (max overlap ≤ cap);
  every kill recorded + requeued; parallel beats the serial floor.
- Scheduler + worker share one log tree (spawn pins `resume_dir` /
  `log_root`); a mid-run kill resumes from completed steps with all
  artifacts under the caller's `--log-root`.

## Memory layer (MCP)

- `query_structure` — tree-sitter code graph (functions, classes,
  calls, imports; persistent index)
- `query_decisions` / `record_decision` — decision/pattern memory
  (auto-ingests every task's structured state)
- `task_status`, `list_repos`

Run it: `python -m mcp_server` (stdio) and connect any MCP client. The
harness's retrieval consumes the same graph programmatically, so
structural context rides into prompts without re-reading files.

## Demo script (5 minutes)

Two variants: **zero-setup offline** (deterministic, no key/Docker) and
the real-model walkthrough.

```bash
# 0) The whole story, offline in one command (scripted model; the loop,
#    verifier gate, git output, rationale, and memory are all REAL):
python demo/run_demo.py        # fix -> git/PR -> routing numbers -> memory -> dashboard hint
#    per-step talking points: demo/README.md
```

With a real model (BYO endpoint/key):

```bash
# one-time: pip install -e .  (or use python -m cli everywhere)

# 1) Fix a real bug with a real model (needs a BYO endpoint/key):
export MY_KEY=...          # your openai-compatible router key
python -m cli fix \
  --repo cli/fixtures/smoke_repo \
  --issue "The mean() function in mathutil.py returns the sum instead of the arithmetic mean. Fix it so tests/test_mathutil.py::test_mean passes." \
  --provider openai --model <model> --api-key $MY_KEY --api-base <base-url>
# → status, cost, diff; then inspect the artifacts:
python -m cli status --task-id <task_id>          # plan checklist + decisions
type logs\<task_id>\rationale.md                  # what was wrong / what changed / why
type logs\<task_id>\git.json                       # branch + commit + PR description

# 2) Adaptive routing vs always-expensive, measured (the ablation):
#    endpoints/keys are configured in runtime/ablation.py (BYO, env keys)
python -m runtime.ablation --tasks all --concurrency 2   # both arms, one summary
type logs\ablations\<ts>\summary.json                    # per-arm cost/token table

# 3) Concurrency + crash-resume at target scale (offline, scripted model):
python -m runtime.stress --mode real --tasks 45 --concurrency 45 --kill 8

# 4) The memory layer, queried over MCP (our own server, external client style):
python -m cli mcp call "python -m mcp_server" query_decisions --args "{\"query\": \"pytest\"}"
python -m cli mcp list-tools "python -m mcp_server"

# 5) Read-only dashboard over any run's logs:
python -m cli dashboard --logs-dir logs/ablations/<ts>/tasklogs
```

## Repo layout

```
harness/      agent loop: planner, steps, verifier gate, resume, git output
execution/    Docker sandbox + verify + git-native output + rationale
runtime/      scheduler, worker, checkpoint, router (novel mechanism), ablation
memory/       code graph (tree-sitter), decision store (SQLite), MCP client
mcp_server/   MCP exposure of memory/status (stdio)
cli/          the `harness` command (fix / run-benchmark / status / mcp / dashboard)
dashboard/    read-only web view of existing logs
demo/         one-command offline demo + walkthrough (run_demo.py)
tests/        ~300 tests incl. real e2e bug-fix runs and real process-kill resumes
logs/         (gitignored) per-task state, traces, ledgers, run journals
```

## Status & verification

- Full test suite green (scheduler integration with real process
  kills, router, memory, MCP incl. real stdio round-trip, dashboard).
- Contract between modules: `INTERFACES.md`. Module-by-module state
  (what's built, what's stubbed, decisions): each module's
  `AGENTS.md`.
- Deferred per spec: SWE-bench Lite numbers (Phase 6), multi-language,
  plugin marketplace.

## Tech

Python 3.10 · litellm (multi-provider, BYO-key) · Docker · tree-sitter
· MCP (official Python SDK) · argparse CLI · stdlib HTTP dashboard.
`litellm` is a real-model dependency (in `pyproject.toml`) pinned to
`1.74.9` on Python 3.10 (newer breaks the `typing` import on 3.10);
the offline/demo paths work without it (lazy import).
