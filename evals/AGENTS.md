# AGENTS.md — evals/: the internal prompt-regression eval harness

(Observability & Eval round, Task B. Read the root AGENTS.md and
INTERFACES.md first — this module is cross-cutting by design but owns
NO boundary; it CONSUMES harness.core.run_task, the Docker sandbox/
verifier, runtime's scripted-model mechanism, and harness config keys.)

## What this module is

A small, FIXED evaluation task set + a paired-arm runner that answers
one question quickly: **did a prompt (or prompt-adjacent config) change
break anything that used to work?** Same discipline as
runtime/ablation.py's paired arms, applied to prompt engineering instead
of the routing mechanism.

## How to run it (the standard pre-ship gate for ANY prompt change)

```powershell
# host self-check of the task set itself (no Docker, ~30s):
python -m evals.run --check

# quick gate — fixture tasks only (fast):
python -m evals.run --quick

# the full matrix — 12 tasks x 6 arms, real Docker loop (~15 min):
python -m evals.run

# just some tasks / arms (dev loop):
python -m evals.run --arms baseline,no_lint --tasks bug02_mean,eval_lint_undefined

# machine-readable (CI):
python -m evals.run --json     # exit 0 = CLEAN, exit 2 = REGRESSIONS
```

Docker must be up for the arms (the verifier is real); `--check` and
`--quick`'s host checks are Docker-less. Reports land at
`logs/evals/<ts>/eval_report.json` (+ per-arm/per-task log trees with
full traces under `logs/evals/<ts>/<arm>/<slug>/`).

## The task set (12, fixed — changing it invalidates comparisons)

| slug | source | what it guards |
|---|---|---|
| bug01_wrap … bug05_cart | the 5 original fixtures (tests/fixtures/) | the DoD set every e2e suite pins — comparability across project history |
| eval_strip_boundary | synthesized | string-boundary bug class |
| eval_wrong_operator | synthesized | wrong-operator class |
| eval_wrong_constant | synthesized | wrong-constant class |
| eval_lost_guard | synthesized | lost-exception-guard class |
| eval_repair_retry | scenario | the REPAIR loop: attempt 1 lands a SYNTAX error, attempt 2 fixes it (requires attempts>=2 — feedback must carry) |
| eval_docs_lookup | scenario | the DOCS escape: mid-step DOCS lookups must not break the loop (both docs-on/off arms green) |
| eval_lint_undefined | scenario | the LINT gate: attempt 1 is an UNDEFINED NAME (valid syntax — only the lint gate sees it), attempt 2 fixes it |

Synthesized/scenario repos are REBUILT FRESH under
`logs/evals/repos/` on every run (`all_tasks()` is deterministic; test-
pinned byte-identical). Fixture repos are read-only committed trees.
**Every repo is written with explicit LF newlines** — the scripted
fixes are seds that run in the LINUX sandbox, and a CRLF file breaks
`$`-anchored sed expressions inside the container while still passing
host-side checks (the exact false-negative that cost a debugging cycle;
see "Bugs found by its own harness" below).

## The arms (config dicts, NOT code forks)

`baseline` = everything on (what ships). `no_memory` / `no_lint` /
`no_docs` / `no_agent_tests` = one improvement-round feature off each.
`pre_round` = the whole round off (pre-improvement prompt surface).
The `_ROUND_KEYS` list in run.py MUST stay in sync with
harness/config.py defaults — test-pinned (`test_runner_arms_use_real_
config_keys`). Adding a new prompt feature: add the config key to
harness/config.py, add an arm (or extend pre_round), update `_ROUND_KEYS`.

## Scoring (per task x arm)

- `status/verified`: from the REAL TaskResult (target + regression +
  not-flaky through the real Docker verifier)
- `attempts`: retry count (repair/docs/lint scenarios REQUIRE >= 2 —
  their whole point is the feedback loop carrying)
- `integrity`: machinery checks on the task's own trace.jsonl —
  task_start AND task_end AND result present; no plan_parse_error;
  no nudge loop (>3 no-command nudges); files_touched recorded
- A REGRESSION = any task/integrity check that worsens vs baseline;
  the report names the exact moved check. Timing is reported, never
  gated (Docker warm/cold noise).

## Determinism notes (honest)

Scripted models make replies identical across arms (offline, zero
network); the loop, sandbox, and verifier are REAL. Wall-clock varies
with Docker warmth — that's why timing never gates. The memory arm gets
an ISOLATED pre-seeded decision store (one relevant + two noise rows;
`HARNESS_DECISIONS_DB` pinned per task, popped after) so
memory-informed planning is reproducible without touching the
production store.

## Bugs found by its own harness (the meta-result — keep these)

1. **Two-command script replies silently drop the second command.**
   The step contract is ONE bash command per turn; `_extract_command`
   beheads multi-command replies. The original eval_lint_undefined
   attempt-2 script was two seds — the real fix sed was dropped and the
   task failed with a confusing verify error. Now test-pinned:
   `test_scripted_replies_are_single_commands` in
   tests/test_evals_tasks.py fails any multi-line non-fenced scripted
   reply at task-set build time.
2. **CRLF repos false-pass host checks, false-fail in-sandbox.** The
   `$`-anchored sed `s/return status$/return status.lower()/` matched
   nothing inside the Linux container (`return status\r`), while host
   Git-Bash sed tolerated CRLF — so `--check` was green and the real
   loop failed. Fixed by LF-forcing `_build_repo` (+ dropping the
   anchor) and pinned by `test_rebuild_is_deterministic`.
3. **Shared-repo pollution false-pass.** Before the rebuild-fresh fix,
   a prior run's fix sat in `logs/evals/repos/<slug>/` and the next run
   "passed" pre-fix (baseline verify green, zero attempts). Now every
   run rebuilds and `test_synthesized_repos_are_buggy_pre_fix` runs the
   target host-side on the built repo.

## Unified tracing integration

`run_eval` defaults `VEX_TRACE_DIR` to the eval logs root;
`_run_one` re-points it per task (`<arm>/<slug>/_trace/`) so arms never
interleave one task id's stream. Reconstruct any task's full lifecycle
(arms + scenarios included):

```powershell
python -m shared.traceview <slug> --logs-root logs\evals\<ts>\<arm>
```

## Files

| File | Role |
|---|---|
| `tasks.py` | the fixed task set: fixtures + synthesized + scenarios; `all_tasks()`, `check_set()` (host self-verify) |
| `run.py` | the paired-arm runner: ARMS, `_run_one` (real loop + scoring + integrity), `run_eval`, CLI (`--check/--quick/--arms/--tasks/--json`) |

## Tests

`tests/test_evals_tasks.py` (8, Docker-less): task-set invariants —
unique slugs, required keys, retry declarations, single-command script
replies (bug class 1 above), arm-key/config-key sync, pre_round
completeness, pre-fix-buggy repos (bug class 3), deterministic rebuilds
(bug class 2). The arms themselves are validated by running them.

## Not yet implemented / deliberate scope

- No real-model arms (the eval measures PROMPT-surface machinery with
  scripted replies — deterministic by design; real-model quality is
  the ablations' job, not the eval's).
- Self-critique is NOT in the arms: the self-critique feature's
  harness half (`render_self_critique_prompt`) never landed (a parallel
  session's in-flight test file test_self_critique.py cannot even
  collect). The arms cover the four LANDED round features:
  plan_with_memory, lint_gate, docs_lookup, agent_tests. When
  self-critique lands, add its key to `_ROUND_KEYS` + a `no_critique`
  arm — that's the whole procedure.
- No per-prompt-version diffing (arms are config-key toggles; if a
  prompt TEXT changes, baseline-vs-baseline across commits is the
  comparison — run before/after and diff eval_report.json).
