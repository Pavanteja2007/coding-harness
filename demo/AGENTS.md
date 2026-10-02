# demo/ — Terminal 4: One-Command Demo (Round 4, Task D)

The interview-demo packaging: a deterministic offline run of the whole
system + the with-a-real-model walkthrough. This is what turns the
project into something demoable, not just a passing test suite.

## What's built
- **`run_demo.py`** — `python demo/run_demo.py`, no API key / network /
  Docker needed. Five steps, each printing its own evidence:
  1. FIX — `harness fix` (real CLI, real `run_task` loop) on the smoke
     fixture's real bug (`mean()` returns sum). Model is the offline
     ScriptedDemoModel (the e2e tests' pattern); sandbox is the local
     subprocess stub (`harness._stubs.sandbox`) so the run is
     deterministic anywhere — same loop, different executor; the
     production path uses the real Docker sandbox (documented in the
     output itself so nobody is misled).
  2. GIT/PR — branch + `[fix]` commit + PR description from `git.json`,
     the rationale paragraph, and `git log` showing the
     pristine→fix two-commit story.
  3. ROUTING — reads the REAL ablation artifacts under `logs/ablations/`
     (5-task and 16-task scales, with the proxy-pricing honesty note);
     degrades to a pointer to `runtime.ablation` when absent.
  4. MEMORY — ingests the demo run's decisions (exactly what MCP
     `query_decisions` does), queries them, then a real `callers
     run_task` code-graph query against this repo.
  5. HINT — the dashboard + concurrent-run combo, with what to watch.
- **`README.md`** — the same walkthrough with a REAL model (reference
  commands + the real-run numbers from cli/AGENTS.md Round 2), a table
  mapping each demo step to its artifact, and per-step talking points
  (what each artifact PROVES — the interview framing).

## Isolation guarantees (verified)
- All state under `demo/demo-work/` (gitignored, regenerated per run):
  `HARNESS_HOME` + `--log-root` point the run at the demo tree.
- Production `.harness/` verified untouched after demo runs (189-row
  decision DB unchanged; no demo task dirs in production `logs/`).
- Gotcha found while building it: `harness.core.run_task` resolves its
  log root from `--log-root` or CWD (`cfg["work_subdir"]`) — it does NOT
  read `HARNESS_LOGS_DIR` (that env var only steers the CLI's status/
  memory/dashboard commands via `memory.paths`). The demo passes
  `--log-root` explicitly; anyone scripting `harness fix` should too.

## Verified by
Two consecutive full runs, exit 0 both times, artifacts checked
(state.json with decisions, git.json with branch/commit/PR, rationale.md,
work/.git with the two-commit log). No dedicated pytest file — the demo
IS an executable check of the integration story; keep it runnable in
CI-ish manual passes (it is intentionally environment-free).

## For the other terminals
- The demo reads YOUR artifacts read-only (T3's ablation summaries,
  T2's git.json/rationale.md shapes). If those formats change, the
  demo's steps 2/3 degrade gracefully (sections omitted) rather than
  crash — but a heads-up in the Change Log lets me update the framing.

## Round 5 (2026-09-09) — CLOSEOUT
- **16-task story re-pointed at the CANONICAL artifacts**: the old
  `v3-expanded/summary_reconstructed.json` reference quoted the run
  runtime/AGENTS.md declares INVALID (fixture-path bug; only `v3-expanded-fixed`
  and `v4` are canonical). Step 3 now reads `logs/ablations/v4/summary.json`
  (both arms, one artifact); demo/README.md paths updated to match. Demo
  re-run green post-change with the v4 numbers rendered.
- Dead code removed (an unused `_arm()` helper from a refactor).
- The demo is unaffected by runtime's Round-4 fake-path fix (it drives the
  real in-process run_task + `--log-root`) and by T1's Round-5 RECALL
  (additive mechanism; the offline scripted model doesn't emit RECALL).

## VEX-PRODUCT-12 — documentation, dogfooding, and product polish (2026-09-25)

### Implemented

- Added the current user guide under `docs/`: quickstart, provider/router
  setup, command and slash reference, workflows, permissions/sandbox,
  extensions, sessions/recovery, headless/CI/SDK, troubleshooting,
  architecture/event schema, feature matrix, and dogfood evidence.
- Updated `README.md` and `CHANGELOG.md` to distinguish `neo fix` from the
  live interactive agent, point to the guide, and avoid claiming a green
  repository-wide gate while the shared tree is dirty.
- Repaired `demo/run_demo.py` so the code-graph example queries the isolated
  demo repository (`demo/demo-work/repo`) instead of indexing the entire
  checkout. The previous version completed the fix and then hung in the
  memory step; the current script exits 0.
- Added `demo/test_product_docs.py` as a fast regression check for required
  guide files, command-registry coverage, and the bounded graph query.
- Added `docs/dogfood-report.md` and the machine-readable handoff at
  `logs/architecture-round/terminal-12.json`.

### Verification and evidence

- `python demo/run_demo.py`: exit 0; 5 scripted model calls, 750 reported
  tokens, 14.5 seconds in the latest run; artifacts under `demo/demo-work/`.
- `python demo/agent_demo.py`: exit 0; all 7 interaction steps passed in
  about 2 seconds; artifacts under `demo/demo-work-agent/`.
- `python -m evals.run --suite daily-driver --json`: 52/52 selected arms
  passed; real Docker canary `completed_verified`; 26 valid comparisons;
  feature evidence 26/26; 16/17 quality capabilities observed.
- Current deterministic summary: 20 `completed_verified`, 25
  `completed_unverified`, 5 `blocked`, 2 `failed`; zero false verified
  successes, unauthorized mutations, lost edits, permission failures,
  resume failures, or UI stalls; p50 latency 1074.742 ms, p95 3300.405 ms;
  trace-to-UI p50 1132.13 ms, p95 1182.681 ms; 5150 tokens; $0.0249 total
  scripted/canary cost; observed user interventions 0.
- `python -m evals.run --suite prompt-regression --check --json`: 14/14
  `CLEAN`.

### Honest blockers and handoffs

- The baseline daily-driver report is `NOT_READY` because the live-provider
  lane was not selected, LSP diagnostic repair is not fully wired into the
  daily-driver boundary, and the three historical real-repository samples
  have no explicit manual-repair booleans. Manual-repair rate is therefore
  `null`, not 0%.
- An explicit live-provider canary was attempted twice. The first lacked a
  base URL; the second used the configured TokenRouter-compatible endpoint
  and was rejected because the token had no access to the selected model.
  No credential value was printed, persisted, or counted as a pass.
- The public PyPI package can lag the 0.2.1 source candidate. `agent_sdk` is
  source-only until packaging adds it to the explicit wheel package list.
- Terminal 12 recorded cross-repository/same-id resume identity as a blocker.
  It is now closed by versioned repository/request/revision/run-namespace
  checkpoint binding; the historical terminal-12 JSON remains an audit
  snapshot of the pre-closure state.

Machine-readable handoff: `logs/architecture-round/terminal-12.json`.
