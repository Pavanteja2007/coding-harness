# dashboard/ — Terminal 4: Read-Only Web Dashboard (spec item 40)

A thin visualization layer over structured logs the system already
writes — deliberately NO new backend, NO database, NO write path.
Promoted from stretch to demo-critical in Round 4: the concurrency +
routing story is the project's differentiator and a live web view
conveys it far better than terminal output (spec's own rationale for
item 40).

Spec's required data dimensions — all present (Round-4 audit vs
project-spec.md Interface section):
- **live task status** → per-task `status` column, color-coded,
  auto-refresh every 5s (5s-granularity live view; crashed workers
  surface via checkpoint fallback before any trace exists);
- **per-agent cost/model used** → `cost_usd` column + `models` map
  (per-model × call counts from the runtime ledger) + aggregate cost
  cards; the difficulty-hint distribution column shows the routing
  decision trail;
- **pass/fail counts** → aggregate cards (success / failed / err-t-o)
  + per-run group headers with per-group totals.

## What's built
- **`collect.py`** — the read-only scanner. `scan_logs(logs_dir)` walks
  the logs tree at ANY depth (rglob, same pattern as memory's
  decision-store poll — ablation/stress/benchmark drivers that stage
  tasks under `<logs>/<driver>/<run>/tasklogs/<id>/` are all found) and
  yields one summary per task dir: status, attempts, cost, model calls,
  per-model call counts, difficulty-hint distribution, plan progress,
  elapsed time, issue snippet, run grouping. Data sources per task:
  - `state.json` + `trace.jsonl` (Boundary 4 — Terminal 1): status,
    cost, attempts (last `result`/`task_end` event), issue text +
    timestamps (`task_start`).
  - sibling `{task_id}.runtime/model_ledger.jsonl` +
    `checkpoint.json` (Terminal 3's layout): per-model counts, hint
    distribution, ledger-cost fallback, and the ONLY source of truth for
    tasks whose worker died before the harness wrote a trace.
  - Degrades, never raises: missing/broken files → status "?", cost 0.
  Also: `group_by_run()` (run buckets), `aggregate()` (pass/fail/cost
  rollups + err/t/o/"?" split).
- **`server.py`** — stdlib-only HTTP server (`python -m dashboard` or
  `harness dashboard`). GET-only by design (POST/PUT/DELETE → 405; the
  handler class has no write code at all), loopback bind by default,
  background refresh thread re-scans every N seconds (default 5) and
  handlers read the atomically-swapped snapshot. Serves one
  auto-refreshing page (inline CSS/JS, no build tooling) +
  `/api/tasks` JSON. Tables: per-run task list with status colors, cost,
  models×calls, hints, plan %, elapsed; aggregate cards (tasks,
  success, failed, err/t/o, cost, model calls).
  HTTP/1.1 with explicit `Content-Length` on every response — under
  1.0, the keep-alive/close race intermittently aborts client reads on
  Windows (real bug found + fixed via a 30-iteration repro loop).

## Verified by
`tests/test_dashboard.py` (9): collector against the REAL combined log
layouts (state+trace+ledger+checkpoint), nested ablation layout,
failed/crashed-no-trace tasks, group/aggregate math, and a real HTTP
round-trip — page + JSON API shapes, POST→405, path traversal →404,
live refresh picking up a task written after server start.
Also smoke-tested against the PRODUCTION logs tree (Round 3 numbers;
Round 4 re-smoke: 878 task dirs, 527 success / 1 failed / 33 error /
315 unknown, $0.6452, 1152 model calls, ~26 run groups — scanned in
1.6s after the Round-4 perf fix below).

## Round 4 — audit + production smoke (re-verified)
- All 9 tests re-run green post Round-3/4 changes; spec's three data
  dimensions confirmed present (see header note).
- **REAL performance bug found + fixed at production scale**: the full
  logs tree had grown to ~880 task dirs containing ~12,400 directories
  (each task dir embeds pristine/ + work/ COPIES of the repo), and the
  5s "live" refresh was taking **67s per scan** — the dashboard wasn't
  live anymore. Two fixes in collect.py:
  1. `_tail_trace_event` claimed to scan traces "from the end" but did a
     full read_text() of every trace.jsonl — now genuinely tail-chunked
     (64KB reverse chunks, partial-line carry).
  2. scan_logs used `root.rglob("state.json")`, which cannot prune — it
     scandir'd every dir inside every copied repo. Now an os.walk with a
     `_SKIP_DIRS` prune set (pristine, work, caches, .git, ...; state
     files never live in those per the documented layout — verified
     against the production tree: 0 state.json files under skipped dirs,
     all 878 found post-fix).
  Result: **67s → 1.6s** on the production tree; the 5s refresh is live
  again. Regression-covered by the existing scan tests + the ground-
  truth check above (logged here for reproducibility).
- One stale framing fixed: README/AGENTS no longer call the dashboard
  "stretch only" — it is demo-critical (still minimal by design).

## Decisions / notes
- Read-only is a design property, not a toggle: no code path writes, and
  the HTTP verb surface is GET-only. Keep it that way if extending.
- Refresh thread owns the scan; handlers read a snapshot — a slow scan
  never blocks requests, and a bad scan can't crash the server.
- No websockets/SSE: `setInterval` polling of `/api/tasks` is enough for
  a 5s-granularity view and keeps everything stdlib.
- Deferred: per-task drill-down pages, charts beyond the aggregate
  cards, filters — polish, per spec "do not let UI polish compete".

## Not yet implemented
- Auth (none — loopback-only read view; anything that can reach it can
  already read the logs dir).
- HTTPS (same reasoning).
