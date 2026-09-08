# dashboard/ — Terminal 4: Read-Only Web Dashboard (spec item 40, stretch)

A thin visualization layer over structured logs the system already
writes — deliberately NO new backend, NO database, NO write path.
Built in Round 3 as the optional stretch task (Tasks A-C done first).

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
Also smoke-tested against the PRODUCTION logs tree: 250 real tasks
visualized (221 success / 1 failed / 21 error / 7 unknown, $0.2397,
119 model calls, 12 run groups incl. all stress + ablation runs).

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
