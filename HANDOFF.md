# HANDOFF.md — Project Closeout (Round 5, 2026-09-09)

Status: **functionally complete against the original scope** in
project-spec.md, minus the deliberately deferred full benchmark run
(SWE-bench Lite = recorded Phase-6 next step). Nothing quietly dropped;
every deferral is a spec-named stretch/future item or a documented,
accepted limitation (see "Honest boundaries" below).

## Final system verification (Round 5, Terminal 4)

One real task through the COMPLETE real pipeline, every module's
closeout fix in the loop — CLI (`python -m cli run-benchmark`) → real
scheduler → worker subprocess → real harness (RECALL + state-fix live)
→ real Docker sandbox/verify (three-valued flake fix) → real cloud
model (z-ai/glm-5.3-free @ tokenrouter) → **success, 1 attempt,
$0.0131, 116s**, with git branch/commit/PR (fix diff IS the fix),
grounded rationale.md, state.json agreeing with the verified result,
original repo untouched, memory auto-ingested, and the decision query
answered over a real MCP stdio round-trip.
Driver + report: `logs/final-e2e/` (`final_e2e_report.json`).

The first attempt of that test failed for the RIGHT reason (driver's
subset named a nonexistent target test; the model fixed the bug anyway
and the verifier gate refused success) — verifier gating works as
designed under a bad target.

## Where the proof lives (read the module AGENTS.md files first)

| Module | Owner | Closeout state |
|---|---|---|
| `harness/` | T1 | 107 tests. All spec items 1–17 SOLID incl. item 13 (RECALL reinjection, Round 5). DoD on 5 fixtures + jaraco/path (real OSS). |
| `execution/` | T2 | 73 Docker-gated tests. Sandbox concurrency hardening, three-valued flake fix, git output + rationale. DoD on python-semver 15/15. |
| `runtime/` | T3 | 48 tests + 4 stress scenarios all-pass at Round-5 closeout. Adaptive routing ablated v1→v4 (2.6–3.5× cheaper at equal success on clean runs). |
| `memory/` + `mcp_server/` + `cli/` + `demo/` + `dashboard/` | T4 | 45-test module sweep (part of repo-wide 300-pass). Items 21–23, 30, 31, 40 all built; demo + README disk-verified. |

Repo-wide test state at closeout: **300 passed, 3 self-skipped
(cloud-key smoke), 0 failed** (runtime's Round-5 full-suite run).

## Honest boundaries (documented, accepted)

- Failure classification (spec 24/25) not implemented — naive-retry
  with rich feedback at the documented `last_feedback` seam; the chosen
  novel mechanism was adaptive model routing (T3), measured 2.6–3.5×
  cheaper at equal success (v2–v4; v1 was an honest negative).
- Ablation numbers are proxy-priced (free-tier BYO endpoints; deltas
  are price-model deltas), n=5/16 × 1 rep — directional, recorded in
  every summary.json. Phase 6: SWE-bench subsets, paid tiers, reps.
- RECALL/retrieval are substring/keyword, not semantic (embeddings =
  documented future seam). Name-based call-graph resolution is the
  documented over-approximation.
- Poetry/pdm/conda repos need manual dep-image builds; SWE-bench
  loader, `--json` CLI mode, web UI = deferred per spec.

## Commit policy

By design, nothing was committed during the build (4 parallel
terminals, one working tree). The Round-5 milestone commit (single
commit, user-confirmed) captures the whole tree; per-module history
lives in each AGENTS.md's round-by-round log, and run evidence lives
under `logs/` (gitignored).
