# demo/ — the 5-minute walkthrough

`python demo/run_demo.py` runs the whole system offline (no API key, no
Docker, deterministic — the model is scripted, everything else is the
real loop). This README covers the same walkthrough WITH a real model
plus what to say at each step.

## What the offline demo shows (and where each artifact lives)

| Demo step | What you see | Artifact / command |
|---|---|---|
| 1. fix a real bug | plan → edit → verify, verifier-gated success, minimal diff | `demo-work/logs/demo-fix-mean/` (state.json, trace.jsonl, diff) |
| 2. git-native output | branch, `[fix]` commit, PR description, grounded rationale | `.../demo-fix-mean/git.json`, `rationale.md`, `git -C .../work log` |
| 3. routing savings | same success, fraction of baseline cost (5-task + 16-task) | `logs/ablations/v2-heuristic-*`, `logs/ablations/v4/` |
| 4. memory over MCP | decision memory queries + code-graph `callers` query | same calls the MCP tools make; `python -m mcp_server` for real clients |
| 5. live dashboard | statuses flip as the verifier decides; cost/model columns | `harness dashboard` + `harness run-benchmark` |

## With a real model (the interview version)

```bash
# 1. Fix a real bug with a real model (any litellm provider, BYO key)
harness fix --repo cli/fixtures/smoke_repo \
            --issue "mean() in mathutil.py returns the sum instead of the mean." \
            --target-test tests/test_mathutil.py::test_mean \
            --model <name> --provider <provider> --api-key <key>

#    Real-model reference runs from this repo (logs/ kept):
#    - cloud model: success, 1 attempt, 7 calls, $0.0093, correct minimal diff
#    - benchmark via scheduler: success 1/1, $0.0031

# 2. Show the product-grade finish
type logs/<task-id>/git.json          # branch / commit / PR description
type logs/<task-id>/rationale.md     # what was wrong, what changed, why
git -C logs/<task-id>/work log --oneline   # pristine commit -> [fix] commit
git -C logs/<task-id>/work show            # the diff IS the fix

# 3. Show the measured routing savings (real ablation artifacts)
#    5-task:  logs/ablations/v2-heuristic-{off,on-r2}/summary.json
#    16-task: logs/ablations/v4/summary.json  (canonical both-arms run)
#    Re-run at will: python -m runtime.ablation --help

# 4. Query memory like an external MCP client would
python -m mcp_server                 # then connect Claude Code / Cursor
#   query_decisions "test suite"     -> real facts learned from past runs
#   query_structure "callers run_task" -> code graph, no file reads
#   (terminal-only variant: harness memory query-decisions /
#    query-structure --repo <path>)

# 5. Watch a concurrent run live
harness run-benchmark --subset <tasks.json> --concurrency 10   # terminal 1
harness dashboard                                               # terminal 2
```

## Talking points per step (what each artifact proves)

1. **Verifier-gated completion** — status is `success` only because the
   target test passed AND the full suite passed AND the test isn't
   flaky. The model never gets to declare done (`trace.jsonl` has every
   prompt/response/tool call if anyone wants to audit the loop).
2. **Not a benchmark script** — the fix ships as a real branch + commit
   with a meaningful message, a PR description containing the diff and
   the verification evidence, and a grounded rationale paragraph built
   from the trace. The pristine→fix two-commit history means
   `git show` of the fix commit is exactly the fix.
3. **The novel mechanism is measured** — same success rate with routing
   on at a fraction of the cost, across two scales; difficulty hints in
   the ledger show WHY each call went where it went. Honesty notes in
   the summaries (proxy pricing, directional success deltas) — say them
   out loud; rigor reads better than inflated numbers.
4. **Memory is real and cross-session** — decisions were auto-ingested
   from state files the runs wrote earlier; the code graph answers
   structural questions without reading files. Any MCP client can query
   it — it's not locked to this harness.
5. **Concurrency is visible** — the dashboard shows tasks appearing,
   statuses flipping (verifier-decided, not model-claimed), per-model
   call counts and cost accumulating — the routing story at a glance.

## Notes

- The offline demo swaps in the local-subprocess sandbox stub
  (deterministic, Docker-free). `harness fix` in normal operation uses
  the REAL Docker sandbox (fresh container per command, no network by
  default). Both paths run the identical harness loop.
- `demo/demo-work/` is regenerated on each run and gitignored; keep the
  last run for inspection, delete freely.
