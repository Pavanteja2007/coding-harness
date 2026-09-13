# coding-harness

[![CI (harness + runtime)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/ci.yml)
[![CI (memory + MCP + CLI)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/memory-cli-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/memory-cli-ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-8C1B33.svg)](LICENSE)

An AI coding agent harness that fixes real software bugs end-to-end:
one system where a Docker-sandboxed agent loop, a concurrent
checkpointing runtime, a persistent cross-agent memory layer (exposed
via MCP), and **adaptive model routing by predicted difficulty** are
integrated deliberately — the integration is the point, not any one
piece.

## Install

One line each, straight from this repo (no PyPI package — the installers
below pull `main` directly via `git+https`):

```bash
# macOS, Linux, WSL:
curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.sh | bash

# Windows PowerShell:
irm https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.ps1 | iex

# Windows CMD:
curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.cmd -o install.cmd && install.cmd
```

What each one does: checks Python 3.10+ is installed (friendly
instructions if not), installs Vex into an isolated environment
(`pipx` if you have it, otherwise a dedicated venv at `~/.vex-venv` /
`%USERPROFILE%\.vex-venv` — never your system Python), puts `vex` on
your PATH, and verifies it actually runs by printing the installed
version. Safe to re-run (upgrades in place). Requirements: Python
3.10+, git, and — only for real bug-fixing — Docker plus a BYO model
endpoint/key (the offline demo needs neither).

Prefer your own package manager? The underlying step is just:

```bash
pipx install git+https://github.com/Pavanteja2007/coding-harness.git
# or, from a clone:  pip install -e .
```

## What it does

```
vex                        # interactive mode, or the subcommands below
vex fix --repo <path> --issue "<bug report>"        # one bug, one agent
vex run-benchmark --subset tasks.json               # N bugs, N supervised agents
vex status --task-id <id>                           # structured progress view
vex dashboard                                       # read-only web view of a run
vex memory query-decisions "<topic>"                # the persistent memory layer
vex mcp call "<server cmd>" <tool> [--args '{..}']  # consume any external MCP server
```

> There is no PyPI package — the installers above (or a clone +
> `pip install -e .`) are the honest install paths. From a clone, the
> legacy alias `harness ...` and `python -m cli ...` also still work.

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
| **Harness** | planner / step agent / verifier gate, repo snapshot + diff, git-native output, rationale log, resume contract | `harness/` ([architecture doc](docs/architecture-harness.md)) |
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
| 5 real OSS repos (Round 6) | always-expensive | 2/5 | 71 | 329,438 | $0.3059 | 2992s |
| 5 real OSS repos (Round 6) | adaptive | 3/5 | 75 | 302,801 | $0.0730 | 581s |

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
n=16 × 1 rep are directional, not benchmark-grade. The Round-6
multi-repo arm additionally suffered endpoint degradation (2 timeouts in
the OFF arm were 0-call wall-clock kills, not model failures). Phase 6
should re-run on SWE-bench subsets with paid tiers.
## Multi-repo validation: the system beyond its home turf

Beyond the fixture set, the full stack has been validated against real,
unfamiliar OSS code — not just the original repo:

- **5 real OSS repos through the routing ablation (Round 6)** —
  more-itertools, arrow, inflect, boltons, python-semver (pinned SHAs,
  one genuine introduced bug each, real suites, per-repo suite pins for
  dev-only deps): the adaptive arm went 3/5 (vs 2/5 always-expensive)
  at **24% of the cost and 5x faster wall** — first multi-repo evidence
  that the routing margin holds (and the failure modes are endpoint
  timeouts, not harness bugs; honest data point: multi-hundred-K-token
  real repos are simply harder than the fixture set, in BOTH arms).
  (`logs/ablations/v6-multirepo/`)
- **jaraco/path (full DoD)** — unfamiliar real OSS repo end-to-end:
  real cloud model, Docker sandbox, verifier-gated success in 1 attempt
  ($0.053, 6 calls), git-native branch/commit/PR, rationale.md,
  approval gate, memory ingestion — plus one honest first failure that
  exposed and fixed a real harness bug (binary-artifact diff crash).
  (`logs/oss-round4/`)
- **python-semver (module DoD)** — pristine baseline → broken-state
  detection → in-sandbox fix → verified, flake-flagging, git output,
  grounded rationale: 15/15 checks. (`logs/dod/`)
- **3 more real repos staged for a final sweep (bottle, click, parse)**
  — full-stack runs in flight; first attempts showed honest failures
  (unparseable plans from the degraded free-tier endpoint → the
  harness correctly refused to claim success). Numbers land when the
  endpoint stabilizes; artifacts will live under `logs/oss-round6/`.

Net: 7 real OSS repos have been driven by the actual harness/verifier
stack (plus 3 in flight), spanning plugin, date/time, inflection, and
versioning domains — with the same verifier-gated honesty rules as the
fixture set: nothing above is a claimed success without the gate.

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
# one-time: pick one of the three install one-liners above
#   (from a clone instead: pip install -e .  — or use python -m cli everywhere)

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

- CI on every push: two workflow files (kept separate — the four
  modules were built in parallel terminals): `ci.yml` (harness +
  runtime suites, OS matrix, nightly full stress + adversarial
  abuse) and `memory-cli-ci.yml` (memory/MCP incl. real stdio
  round-trip, CLI offline e2e through the real Docker sandbox,
  dashboard — across Linux/Windows/macOS). Badges above.
- Full test suite green (scheduler integration with real process
  kills, router, memory, MCP incl. real stdio round-trip, dashboard).
- CI (`.github/workflows/ci.yml`): the harness suite runs on every
  push across Linux/macOS/Windows × Python 3.10/3.12 — Docker-gated
  e2e tests self-skip with an explicit reason on runners without
  Docker; full-scale stress + abuse suites run nightly. Harness
  internals: [docs/architecture-harness.md](docs/architecture-harness.md).
- **Adversarially tested (Round 6)**: the MCP server and CLI were
  probed with crafted/hostile inputs — path traversal, shell-injection
  payloads, SQL injection, malformed subsets, null bytes. One real
  data leak (task-id path traversal in `task_status`/`harness status`)
  was found live, fixed, and pinned by 101 adversarial tests; all other
  surfaces held (per-probe outcomes in each module's AGENTS.md; the
  Docker sandbox was adversarially confirmed separately — 24/24
  sequential + concurrent attack suites).
- Contract between modules: `INTERFACES.md`. Module-by-module state
  (what's built, what's stubbed, decisions): each module's
  `AGENTS.md`. High-level build history: `CHANGELOG.md` (current
  release: **v0.1.0**).
- Deferred per spec: SWE-bench Lite numbers (Phase 6), multi-language,
  plugin marketplace.

## Tech

Python 3.10 · litellm (multi-provider, BYO-key) · Docker · tree-sitter
· MCP (official Python SDK) · argparse CLI · stdlib HTTP dashboard.
`litellm` is a real-model dependency (in `pyproject.toml`) pinned to
`1.74.9` on Python 3.10 (newer breaks the `typing` import on 3.10);
the offline/demo paths work without it (lazy import).

## License

[MIT](LICENSE) — © 2026 Pavanteja2007. Report security issues
privately per [SECURITY.md](SECURITY.md); conduct is governed by the
[Contributor Covenant](CODE_OF_CONDUCT.md).
