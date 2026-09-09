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
