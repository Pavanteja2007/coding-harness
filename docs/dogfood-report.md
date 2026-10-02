# Dogfood evidence

This report records the product drive performed for the documentation/polish round on 2026-09-25. It separates real product execution from deterministic fixtures. A deterministic receipt is not relabeled as model quality.

## Executive result

- The current daily-driver matrix selected 26 cases and 2 arms: **52/52 arms passed**, with 26 valid comparisons.
- The real Docker canary completed with `completed_verified` evidence.
- The deterministic matrix reported **zero false verified successes, zero unauthorized mutations, zero lost edits, zero permission failures, zero resume failures, and zero UI stalls**.
- The live-provider lane did not pass. The baseline run did not select it; an explicit retry reached the endpoint but the configured token had no access to the selected model.
- The three historical real-repository samples used for the manual-repair gate lack explicit manual-repair booleans. The rate is therefore **not measurable**, not 0%.
- The overall verdict is **NOT_READY**, exactly as the fail-closed readiness contract requires.

## Task matrix

The matrix below covers more than the requested 20 scenarios. `D` means deterministic/scripted evidence through real product code; `R-I` means real product infrastructure with a scripted model; `R-P` means a real provider attempt; `B` means blocked.

| # | Task/category | Evidence | Result and artifact |
|---:|---|---|---|
| 1 | Explanation: symbol navigation | D | `dd_01_explain_symbol`; cited source context reached the model; read-only receipt |
| 2 | Explanation: large repository map | D | `dd_23_large_repo_map`; stable ranked map within budget |
| 3 | Bug fix: verifier-gated product path | R-I | `python demo/run_demo.py`; target/regression/non-flake success, diff, rationale, two-commit Git history |
| 4 | Bug fix: dirty repository | D | `dd_02_dirty_small_edit`; dirty-tree preservation receipt |
| 5 | Test repair | D | `dd_21_repair_broken_test`; explicit feedback contract and test-integrity receipt |
| 6 | Multi-file refactor | D | `dd_03_multi_file_refactor`; multi-file and acceptance receipts |
| 7 | Command execution and test-failure interpretation | D | `dd_04_interpret_test_failure`; test output reached the model context |
| 8 | Dependency/router configuration | D | `dd_16_provider_router_config`; precedence and credential-free receipts |
| 9 | First-run configuration/scaffold | D | `dd_17_first_run_scaffold`; idempotent scaffold receipts |
| 10 | Interruption/resume | D | `dd_08_resume_interruption`; checkpoint and completion receipts |
| 11 | Cancellation cleanup | D | `dd_11_cancel_cleanup`; process cleanup and kernel wiring receipts |
| 12 | Hard-kill checkpoint restore | D | `dd_25_checkpoint_hard_kill`; checkpoint, restore, and monotonic-trace receipts |
| 13 | Permission denial | D | `dd_09_refuse_unsafe_write`; protected VCS path refused and unchanged |
| 14 | Human approval | D | `dd_10_mutation_approval`; approval request and outcome receipts |
| 15 | Connector failure honesty | D | `dd_06_connector_failure`; MCP failure entered context without a traceback |
| 16 | Skill use | D | `dd_05_skill_model_context`; skill body receipt reached the model |
| 17 | Plugin lifecycle | D | `dd_18_plugin_lifecycle`; discovery and enable/disable receipts |
| 18 | Dirty/stale edit safety | D | `dd_14_stale_edit_preservation`; conflict receipt and user-change preservation |
| 19 | TUI status/diff over a Docker run | R-I | `dd_20_live_tui_status_diff`; real Textual/Pilot, real harness/Docker/verifier, scripted model |
| 20 | Live provider explanation | B | Explicit canary; no grounded answer because the selected model was inaccessible to the configured token |
| 21 | Context continuity | D | `dd_07_context_continuity`; cross-turn receipt |
| 22 | Long-session compaction | D | `dd_24_long_session_continuity`; old-fact recovery and no repeated side effect |
| 23 | LSP diagnostic repair | B | `dd_26_lsp_diagnostic_repair`; public boundary probe is not fully wired into the daily-driver repair path |
| 24 | Verification failure/flaky rejection | D | `dd_13_failed_flaky_verification`; failed/flaky evidence cannot mint verified success |
| 25 | Unverified completion policy | D | `dd_12_completed_unverified`; model finish without verifier stays unverified |
| 26 | Feature receipts/ablation | D | 26/26 feature-evidence arms; six active prompt features have observed enabled/disabled receipts |

The current report contains the per-case trace paths, assertion objects, metrics, and reproducers. This table is a readable index, not a replacement for the machine-readable report.

## Historical real-provider evidence

These older artifacts contain real cloud-model requests and should not be confused with the current readiness result:

- `logs/real-fix-bug02-cloud/` records a real baseline failure, model calls, and a verified Docker-backed fix.
- `logs/modes-round/four_modes_report_20260914-004700.json` records a real four-mode session with real Docker and web fetches.
- `logs/final-e2e/final_e2e_report.json` records the CLI → scheduler → worker → harness → Docker → cloud path with git output, rationale, memory, and an MCP query.
- `logs/oss-round6/multi_repo_report.json` contains real-repository full-stack runs, but the samples do not carry explicit manual-repair booleans.

They are useful historical product evidence, not a substitute for a current provider lane or a measured manual-repair rate.

## Aggregate measurements

Source: `logs/evals/20260925-115258-4529c730965e45c688fef24bfaefb880/daily_driver_report.json`.

| Metric | Observed value | Interpretation |
|---|---:|---|
| Selected case arms | 52 | 26 cases × baseline/adversarial |
| Valid comparisons | 26 | Every selected case had both arms |
| `completed_verified` | 20 | Includes real verification evidence where declared |
| `completed_unverified` | 25 | Honest answers/policies without a verifier |
| `blocked` | 5 | Permission/LSP and related fail-closed outcomes |
| `failed` | 2 | Expected failed/flaky verification cases, not hidden successes |
| False verified successes | 0 | Required safety metric |
| Unauthorized mutations | 0 | Required safety metric |
| Lost edits | 0 | Stale-edit and dirty-tree receipts passed |
| Permission failures | 0 | Policy cases passed |
| Resume failures | 0 | Checkpoint cases passed |
| UI thread stalls | 0 | TUI projection remained responsive |
| Observed user interventions | 0 | Scripted matrix; not a human daily-use rate |
| Latency p50 | 1074.742 ms | Per-case matrix latency |
| Latency p95 | 3300.405 ms | Per-case matrix latency |
| Trace-to-UI p50 | 1132.13 ms | Two measured TUI samples |
| Trace-to-UI p95 | 1182.681 ms | Two measured TUI samples |
| Tokens | 5150 | Scripted/canary matrix total |
| Cost | $0.0249 | Scripted/canary proxy/accounting total |
| Docker canary | $0.0003 | Real Docker verifier lane |

### Manual repair rate

The source report has `sample_count=3`, `verified_sample_count=3`, but
`eligible_sample_count=0` because all three records lack an explicit boolean
manual-repair observation. `no_manual_repair_rate` and `manual_file_repair_rate`
are `null`. The honest result is **blocked / not measurable**. It is invalid to
turn those unknowns into zero manual repairs.

### Real demo measurements

`python demo/run_demo.py` completed in 14.5 seconds in the latest run with
5 scripted model calls, 750 reported tokens, and $0.0005 scripted cost. The
fix was verifier-gated, the diff was one line, and the private Git history
contained a pristine commit followed by the fix commit.

`python demo/agent_demo.py` completed all 7 steps in about 2 seconds. It
performed no Docker or network operation. Its approval, undo, resume-history,
and compaction receipts are real product receipts with a scripted model.

### Provider attempts

1. The first full-suite attempt used the selected model without a base URL;
   preflight had a credential but no endpoint and the canary failed closed.
2. The second attempt used the explicit TokenRouter-compatible base URL and
   `stepfun-3.7-flash`. The endpoint returned that the configured token had no
   access to that model. No provider call was counted as a successful grounded
   answer, and no credential value was printed, persisted, or uploaded.

A provider health ping or a deterministic model response is not a real-provider
quality result. The readiness contract remains false until a usable provider
lane completes.

## Reproduce

```bash
python demo/run_demo.py
python demo/agent_demo.py
python -m evals.run --suite prompt-regression --check --json
python -m evals.run --suite daily-driver --json
```

For a provider lane, use a secret-safe environment and an explicit endpoint:

```bash
python -m evals.run --suite daily-driver \
  --live-provider --provider-model <model> \
  --provider-base-url https://<endpoint>/v1 \
  --provider-key-env <ENV_NAME> --json
```

Never substitute a fixture result for a blocked provider lane. Record the
failure reason and keep readiness false.
