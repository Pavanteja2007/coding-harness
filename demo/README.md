# Demo guide

The demo directory contains two reproducible, deterministic product walkthroughs. They are useful for onboarding, UI recording, and checking the integration story without a provider or Docker.

## One-command fix demo

```bash
python demo/run_demo.py
```

The script:

1. Copies the smoke fixture into `demo/demo-work/repo`.
2. Runs the real `harness.core.run_task` loop through the CLI on a real bug.
3. Uses `ScriptedDemoModel` and the explicit local subprocess sandbox fallback.
4. Verifies the target test, full suite, and flake status.
5. Shows the diff, rationale, private two-commit Git history, and PR description.
6. Reads canonical routing summaries when present.
7. Ingests and queries decision memory.
8. Queries the structural graph for the isolated demo repository.

The run keeps artifacts under `demo/demo-work/` and exits 0. It is deterministic and needs no API key, network, or Docker. The fallback is intentional; the production `neo fix` path uses Docker.

The structural query is deliberately scoped to `demo/demo-work/repo`. Do not change it back to indexing the whole checkout: a five-minute demo must have a bounded graph step.

## One-minute agent demo

```bash
python demo/agent_demo.py
```

This script drives the live-repository agent with a scripted model and local tools:

```text
question -> @file context -> plan preview -> approval -> edit -> diff -> undo -> resume -> compact
```

It creates and keeps `demo/demo-work-agent/`. It proves the interaction receipts and the session behavior, not Docker isolation or model quality.

## Stable transcript

The generated `demo/neo-demo-transcript.txt` and `demo/neo-demo.gif` are recording artifacts. The stable prose transcript is [`TRANSCRIPT.md`](TRANSCRIPT.md); a generated file is not the source of truth for current feature status.

Representative verified-fix transcript:

```text
neo fix -> demo/demo-work/repo
task:   demo-fix-mean
issue:  mean() in mathutil.py returns the sum of the values instead of the arithmetic mean.
run 31 events · 5 model calls · 750 tokens · $0.000500

task demo-fix-mean: success
attempts:     1
target test:  PASS
regression:   PASS
flaky:        False

--- a/mathutil.py
+++ b/mathutil.py
-    return sum(values)
+    return sum(values) / len(values)

routing: historical 5-task and 16-task proxy-price summaries are printed when present
memory: decisions ingested and queried
structure: func mathutil.mean (mathutil.py:4)
```

The exact run may show different elapsed time, event ordering, commit hash, or historical ablation availability. Treat the evidence labels below as authoritative.

## Real-model walkthrough

With Docker and a configured model:

```bash
neo login
neo fix --repo cli/fixtures/smoke_repo \
  --issue "The mean() function in mathutil.py returns the sum instead of the arithmetic mean. Fix it so tests/test_mathutil.py::test_mean passes." \
  --target-test tests/test_mathutil.py::test_mean

neo status --task-id <task-id> --json
cat logs/<task-id>/rationale.md
cat logs/<task-id>/git.json
```

For concurrent runs:

```bash
neo run-benchmark --subset <tasks.json> --concurrency 10
neo dashboard
```

For memory over the stdio MCP server:

```bash
python -m mcp_server
```

An external MCP client can call `query_structure`, `query_decisions`,
`record_decision`, `task_status`, and `list_repos`. The CLI can also consume
an external server through `neo mcp list-tools` and `neo mcp call`.

## Recording the GIF

The existing recording helpers are optional and platform-sensitive:

```bash
python scripts/make_demo_gif.py
python scripts/render_demo_gif.py
```

`make_demo_gif.py` currently uses Windows `cmd`/`robocopy`; the renderer uses
Pillow. They are not required for the two portable demo commands and are not
a cross-platform release gate. If a recording helper fails, keep the text
transcript and report the optional recording lane as blocked.

## Evidence labels

- `real product`: actual CLI/harness/runtime/Docker path.
- `deterministic`: scripted model or fixture contract.
- `source-only`: present in this checkout but not guaranteed in the wheel.
- `blocked`: environment or integration unavailable.

The offline demos are deterministic. Historical ablation summaries include
proxy-pricing and endpoint caveats. The current dogfood report records which
lanes actually ran and which remain blocked.
