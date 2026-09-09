# cli/ — Terminal 4: User-Facing CLI (Boundary 6)

> **Cross-terminal note (2026-09-09):** the sections below through Round 6
> are Terminal 4's. The "Vex CLI pass" section at the BOTTOM is from a
> different session (Terminal 2, the execution/terminal), which did the
> rename + rich + interactive work described there — coordinated via this
> file and the INTERFACES.md Change Log, building ON TOP of Round 6's
> adversarial hardening (all 62 adversarial tests kept green; only three
> decorative output-string assertions in test_cli.py were updated to the
> new render format, same verification intent).

## What's built
`cli/main.py` — the `harness` command (argparse; entry points: installed
console script, `python -m cli`). Subcommands:

- **`harness fix --repo <path> --issue <text|@file> [--task-id ...]`** →
  calls Terminal 1's REAL `harness.core.run_task` directly (single-task
  mode, no scheduler). Config flags (`--model --provider --api-key
  --target-test --test-command --max-retries --budget --protected
  --log-root`) map into `Task.config` per project convention. Prints a
  result summary (status, attempts, cost, verification flags, diff, trace
  path). Exit codes: 0 success, 1 task failure, 2 usage error.
- **`harness run-benchmark --subset <name|file.json> --concurrency <n>`**
  → calls Terminal 3's REAL `runtime.scheduler.run` (worker subprocesses,
  checkpoint/resume, supervision). `_call_scheduler` signature-probes:
  real scheduler `(tasks, concurrency, logs_root) -> dict` is normalized to
  task order; the local stub (below) stays importable for pre-T3 testing.
  Subsets: `smoke` (offline plumbing check via `use_fake_harness`, per
  runtime's documented config) or a JSON file
  `[{"repo", "issue", "task_id", "config", "target_test"}]` — SWE-bench
  loader is future work (spec defers it).
- **`harness status --task-id <id>`** → renders
  `logs/{task_id}/state.json` (plan checklist, files touched, decisions,
  remaining) + enriches status/cost/note from `trace.jsonl`'s last
  `result`/`task_end` event. Lists available task dirs when the id is
  unknown.
- **`harness memory record|query-deisions|query-structure|ingest`** —
  thin wrappers over the memory layer for demos/smoke (MCP remains the
  programmatic interface).
- **`harness dashboard [--logs-dir ...] [--host] [--port]
  [--refresh-s] [--no-browser]`** (Round 3, spec item 40) — serves the
  read-only web dashboard over existing logs; blocks until Ctrl+C.
  See `dashboard/AGENTS.md`.
- **`harness mcp list-tools "<server cmd>"` / `harness mcp call
  "<server cmd>" <tool> [--args '{...}'] [--cwd <dir>]`** (Round 4,
  spec item 30) — consume any EXTERNAL MCP server over stdio via
  `memory/mcp_client.py` (official SDK client). Results print as text;
  spawn/tool errors → clean stderr message + exit 1, never a traceback.
  Verified: our own `python -m mcp_server` doubles as the external
  target (tests/test_mcp_client.py, 12).

`cli/deps.py` — dependency resolution mirroring `harness/deps.py`:
override → real module → local stub. `run_task` has no stub (T1's real one
is on disk); `scheduler.run` falls back to `cli/_stubs/scheduler.py`
(threads + fan-out, same contract) if `runtime.scheduler` is ever absent.

`cli/fixtures/smoke_repo/` — tiny repo with a real bug (`mean()` returns
sum) + failing test; used by the `smoke` subset and CLI e2e tests.

## Verified by
`tests/test_cli.py` (16): parser, status rendering/enrichment/missing-id,
memory subcommands, full OFFLINE fix e2e (real run_task + scripted model
injected via `harness.deps.set_call_model` — fixes the bug, verifier
passes, diff printed), `@file` issue reading, model-failure → clean
structured error, run-benchmark through the REAL scheduler (subprocess
workers, offline fake harness), unknown-subset handling, stub fan-out +
crash isolation.

## Round 2 — REAL integration results (no stubs, no injected models)
- **`harness fix` e2e (real everything)**: fixed bug02_mean via the real
  `harness.core.run_task` + real Docker-sandboxed verify + REAL cloud
  model (z-ai/glm-5.3-free @ tokenrouter, BYO key) → `success`, 1 attempt,
  7 model calls, $0.0093, 116s, correct minimal diff (divisor fix), real
  state.json + trace.jsonl. A real Ollama qwen2.5:0.5b run was also
  attempted: plumbing perfect (verifier-gated failure, no false success) —
  the 0.5b model just replies SUBMIT without acting; capability limit,
  not a harness bug.
- **`harness run-benchmark` e2e (real everything)**: JSON subset → real
  `runtime.scheduler.run` → subprocess worker → real harness + real cloud
  model → `success 1/1`, $0.0031, 87s.
- **Gotchas found & fixed during real integration**:
  - Workers' default `hang_heartbeat_stale_s` (30s) kills the real harness
    during long model calls (T3's documented granularity limit). Fix on
    the caller side: set `hang_heartbeat_stale_s` in benchmark task
    configs for real-model runs — **Round-4 update (T3's measurement):
    300 is BORDERLINE when the cheap tier is loaded (a first planner
    call ran >300s and got hang-killed, requeued, finished on relaunch);
    use 600+ for real-model runs, 1200 for ablation-scale loads.**
  - `--api-base` flag added (maps to Task.config.api_base → router context
    set by `_set_router_context`, mirroring runtime.worker's exact pattern)
    — in-process fix runs against custom endpoints (BYO routers) work
    without a worker. Also `--adaptive-routing` flag added.
  - Subset JSON loader now reads `utf-8-sig` (PowerShell 5.1 writes BOMs).
  - Repaired `harness/context.py` mid-integration (T1's interrupted edit
    left a dangling `def _write` stub → IndentationError repo-wide);
    minimal deletion only, logged in INTERFACES.md Change Log.

## Notes / decisions
- Offline testing: `fix` is in-process (model injectable); benchmark workers
  are subprocesses (model NOT injectable in-process) — benchmarks go
  offline via `use_fake_harness` in Task.config. Documented in the
  Change Log.
- Exit code contract: 0 ok / 1 task-level failure / 2 usage error — keep
  when adding commands.
- `--issue @file` reads the bug report from a file.
- Router context for in-process fix runs is set/cleared around run_task
  (`_set_router_context` / `_clear_router_context`) — best-effort, no-op
  when runtime's router is absent (stub phase).

## Round 4 — README restored, demo packaged, audit clean

- **Root README.md was accidentally emptied** (commit 983f532 "Updated
  README.md" = 159 deletions, 0 additions — Round 3's content lost).
  Restored from the initial commit and updated for Round 3/4 reality:
  git-native output + rationale bullets, approval mode, DoD-on-real-OSS
  (python-semver), BOTH ablation scales with their honesty notes,
  corrected test count (257), demo section, and a "Status / what's
  honest" section (incl. the item-30 gap). Check `git diff README.md`
  before your next commit — if it still shows the 159-line deletion,
  restore from this version.
- **`demo/` packaged (Task D)**: `demo/run_demo.py` — one command, no
  API key/Docker, deterministic (scripted model + local sandbox stub;
  the harness loop, verify gating, git output, rationale, memory
  ingestion, and MCP-surface queries are all the REAL code paths).
  Isolated under `demo/demo-work/` (gitignored) via HARNESS_HOME +
  --log-root; production `.harness/` verified untouched. Reference a
  real-model variant + per-step talking points in `demo/README.md`.
  Verified: exit 0, artifacts present (git.json/rationale.md/state/
  trace), ran twice for idempotence.
- **Task A audit (this module's inventory lines)**: Boundary 6 CLI —
  fix / run-benchmark / status / memory / dashboard all live against
  real modules; 16/16 CLI tests green in the 254-pass repo-wide re-run.
- No CLI code changes this round (the demo drives it as-is).

## Round 6 (2026-09-09) — adversarial input-validation hardening: 3 real bugs found, fixed, pinned

**Verdict up front: crafted/hostile arguments now never execute
unintended commands, never access unintended paths, and never produce a
raw traceback — 62/62 adversarial tests green
(tests/test_cli_adversarial.py) + a 38/38 contained production session
against the REAL `python -m cli` subprocess
(logs/cli-adversarial/, report JSON kept). But the round started with
three live-confirmed defects.**

### The real findings (all found live, all fixed)

1. **`harness status --task-id` path traversal** (same root cause as
   the MCP server's task_status leak — see mcp_server/AGENTS.md):
   `--task-id C:/Users/.../final-e2e-bug02` rendered any
   state.json-shaped file on the host (pathlib `/` discards the base
   for drive-lettered operands; `..`-chains resolve through).
   Fixed: ids resolve through the SHARED guard
   `memory/paths.py::safe_task_dir` (semantic rejection: separators,
   null bytes, `:`-drive forms, Win32 whitespace/dot-tricks like
   `' ..'` → `..`; plus resolve-containment). Legit odd ids (spaces,
   unicode, interior dots) still work — regression-tested both ways.
2. **Malformed subset JSON crashed with a raw traceback**
   (`{"repo": 123}` → TypeError deep in `_load_subset`; also
   `"config": "string"` → `dict()` ValueError — found when my first
   fix's test run caught the SECOND form). Fixed: full entry-type
   validation (repo/issue non-empty strings, task_id string, config
   object) → clean `error: subset entry N: ...` + exit 2.
3. **Null-byte `repo` in a subset entry spawned REAL doomed workers**
   (found live: a probe subset leaked into ./logs and the scheduler
   crash-retried against the unusable path). Fixed: rejected at
   validation ("repo contains a null byte") before any spawn.
   Side-hardening in the same pass: `--issue @file` handles binary
   (UnicodeDecodeError) and null-byte paths cleanly; `memory
   query-structure --repo` catches OSError/ValueError as usage errors.

### Every adversarial probe tried, and its outcome (all CONTAINED)

- **Shell injection via `--repo`** (`x; touch pwned.txt`, `x && calc`,
  `x | calc`, `` `calc` ``, `$(calc)`, `x" || "calc`, newlines):
  `--repo` is data (a path checked with `is_dir()`), never a shell
  command — argparse receives it as one argv element; no marker files
  ever created (production probes verified by filesystem inspection
  after every payload). The CLI never constructs shell command lines
  from user input: the only subprocess executions are (a) the
  scheduler's worker spawn (fixed argv, no shell) and (b) Docker CLI
  calls (fixed argv, no shell) — verified by grep: zero
  `shell=True`/`os.system`/`eval`/`exec` in the repo.
- **Shell injection via `--issue`** (`; rm -rf / #`, `$(calc)`,
  `` `reboot` ``, `&& shutdown /s`, python code, template/script
  payloads): the issue text is prompt DATA stored in Task.issue_text
  and rendered into the model prompt — never evaluated. (What the
  MODEL does with it inside the sandbox is the harness's deny-pattern
  + sandbox story, T1/T2's verified surface; the CLI boundary itself
  is inert.)
- **`--task-id` traversal** (absolute, `..`-chains, drive-only `C:`,
  `C:x`, separators, null bytes, `' ..'`/`'.. '`): all rejected with
  exit 2 + "invalid task id"; outside-logs canary never rendered;
  `--log-root` doesn't reopen the hole (guard runs against the
  effective root).
- **Malformed subsets** (not-JSON, non-list, `repo`-int/null/empty/
  whitespace, `issue`-int/null/empty, `task_id`-int, `config`-string,
  entry-as-list/string/number/null, null-byte repo, BOM'd valid file):
  clean usage errors, no tracebacks; BOM files still work (utf-8-sig
  kept). Valid-shaped file at an unintended path (`--subset` pointing
  at a state.json) → "must be a JSON list" (the file reader reads
  whatever the operator names — local trust, documented).
- **`--issue @file`** (missing, binary, null-byte path): clean usage
  errors. Note the OS layer: Windows `CreateProcess` refuses null
  bytes in argv entirely (verified live) — two containment layers.
- **memory subcommands** (`record` with DROP TABLE/`$(calc)`/backtick
  payloads: stored verbatim, returned verbatim, store intact;
  `query-decisions` hostile text: inert keyword; `query-structure`
  null-byte repo + traversal queries `file ../../win.ini` → indexed-
  path miss, never host content).
- **Subprocess argv end-to-end** (`python -m cli status --task-id ../…`):
  exit 2, no traceback, no side-effect files.

**Where the guarantees live**: shared task-id guard at
`memory/paths.py` (one implementation for CLI + MCP server);
containment tests: tests/test_cli_adversarial.py (62, incl. parametrized
traversal/injection matrices + real-subprocess test). Production audit
artifact: `logs/cli-adversarial/run_cli_adversarial.py` +
`cli_adversarial_report.json` (38/38, markers: none).

**No contract changes**: all signatures/flags unchanged; exit-code
contract (0/1/2) preserved; the only behavior changes are clean
rejections where tracebacks or out-of-scope reads used to be.



- **Task A (the final system test) — PASS.** Drove the COMPLETE real
  pipeline through this module's own entrypoint: `python -m cli
  run-benchmark` (driver `logs/final-e2e/run_final_e2e.py`, resolves
  TOKENROUTER_API_KEY into the subset config — the ablation's pattern)
  → real scheduler → worker subprocess → real harness (RECALL + the
  exhausted-turns state fix live) → real Docker sandbox/verify (T2's
  three-valued flake fix) → real cloud model → success, 1 attempt,
  $0.0131, 116s. Verified: state.json 1/1 steps complete (the T3-flagged
  flag class stayed fixed), git.json + two-commit work/ repo (fix diff
  IS the fix), rationale.md grounded, original fixture untouched,
  memory ingested (recursive poll, 2 decisions), MCP query answered via
  the real CLI→stdio round-trip, `harness status` renders true progress.
  Report: `logs/final-e2e/final_e2e_report.json`.
  Honest note: the FIRST run failed — but for the right reason: my
  driver's subset named a nonexistent target test (`test_mean` vs the
  fixture's real `test_mean_even_count`); the MODEL fixed the bug
  anyway and the verifier-gate correctly refused success on a bad
  target. Verifier gating worked as designed; driver fixed + re-run.
- **Task B (doc-sync)**: `hang_heartbeat_stale_s` guidance updated to
  runtime's Round-5 measurement (300 borderline under load; 600+ for
  real-model runs, 1200 ablation-scale); stale "stretch item 40"
  dashboard labels dropped (the stretch framing was retired in R4).
- `tests/test_cli.py` + mcp/dashboard suites re-run green this round
  (45/45 module tests); demo re-run green post-fixes.

## Deferred
- SWE-bench Lite subset loader (spec: deferred, not a blocker).
- Global `--json` output mode (human-readable only for now).

## Round 7 (2026-09-09) — Terminal 4: production readiness (CI, CHANGELOG, v0.1.0)

- **CI** (`.github/workflows/memory-cli-ci.yml`, separate from Terminal
  2's ci.yml by design): test_cli.py runs on ubuntu × py3.10/3.12 per
  push — including the offline fix e2e (real run_task → scripted model →
  REAL Docker sandbox/verify), with the smoke_repo dep image warmed
  first. 16/16 green against the committed tree (b9ecd9c).
  Cross-terminal note: the Windows legs of the OTHER job run this
  module's Docker-light suites (memory/MCP/dashboard) — the CLI suite
  is Linux-only in CI because its e2e needs the Docker daemon
  (windows-latest runners don't ship it).
- **CHANGELOG.md + v0.1.0 + README CI badge**: repo-root CHANGELOG
  summarizes all rounds' milestones (incl. Round 6's adversarial pass
  over this CLI: 3 real bugs found/fixed, 62/62 pinned).
- 62/62 adversarial re-run green this round (with test_mcp_adversarial:
  101 + 2 new fileno-regression = 103 total in the combined sweep).
- The Round-6 closeout item (multi-repo README update) was verified
  STILL in flight at round end: multi_repo_report.json holds only a
  scripted-model parse entry (honestly FAILED its verifier gate — the
  staged regression test's own expected spans were miscalculated;
  bottle/click absent; real-model attempts all died on the degraded
  free-tier endpoint) — so the README's honest in-flight wording was
  correctly left unchanged.

## Vex CLI pass (2026-09-09, Terminal 2 session) — rename, rich theme, interactive mode, live UX

*(Side task, not a round — see INTERFACES.md Change Log entry of the same
date. Everything below is from the visiting Terminal 2 session; Terminal
4's content ends at "Deferred" above.)*

### What changed (all 109 CLI-adjacent tests green after: test_cli 16,
### test_cli_adversarial 62, test_cli_vex 10 NEW, test_dashboard +
### test_mcp_client 21)

- **Rename**: project name → `vex` (pyproject `[project] name`), console
  script `vex = "cli.main:main"`, argparse `prog="vex"`, all cli/ docs and
  docstrings. **`harness` stays as a console-script ALIAS** (both install)
  so demo/, older docs, and other terminals' scripts keep working —
  remove the alias only in a dedicated migration commit after every
  terminal's docs are updated.
- **Task A (cross-platform packaging)**:
  - REAL BUG FOUND + FIXED: `pip install -e .` was BROKEN for everyone
    (flat-layout multi-package discovery refuses to build: "Multiple
    top-level packages discovered"). Added explicit `[tool.setuptools]
    packages` for all 10 packages — `vex.exe` + `harness.exe` now both
    install and run (verified live). This also un-broke the Deferred item
    above ("installing needs pip install -e ." — it never actually
    worked before).
  - REAL BUG FOUND + FIXED: legacy Windows consoles (cp1252) CRASHED on
    the first emoji/glyph render (→/›/●/✔ → UnicodeEncodeError in rich's
    LegacyWindowsTerm writer — caught by a manual drive, not by luck).
    Fix: `ui.GLYPHS` — encoding-probed glyph table with ASCII fallbacks
    (->, >, *, OK, x, T), used everywhere glyphs appear.
  - `rich` handles ANSI detection (Windows Terminal/conhost VT/pipe
    degradation) — verified, not assumed: piped output has no color codes,
    `--no-color`/`NO_COLOR` strip color codes (tested in test_cli_vex).
    NOTE: rich's `no_color` still emits **bold** ANSI when forced; the
    color-code strip is what matters for dumb consoles.
  - pathlib was already used throughout cli/ (no os.path.join anywhere);
    zero `shell=True` in cli/ (T4's Round-6 audit held); Docker calls
    flow through execution.sandbox (container-internal Linux bash — host
    OS irrelevant), no Docker path handling existed in cli/ to break.
- **Task B (theme)**: NEW `cli/ui.py` — one shared rich Console with
  `VEX_THEME` (amber/ember: `vex.accent` #e8722a burnt amber, `vex.running`
  #e6b84c soft gold, `vex.ok` green3, `vex.error` red3, `vex.warn`
  orange1, `vex.muted` grey58, `vex.diff.*` roles). Applied to EVERY
  command (fix/run-benchmark/status/memory/dashboard/mcp + interactive),
  not one. Helpers: `console()`/`err_console()` (stderr-bound),
  `set_no_color()`, `fmt_cost()`, `print_diff()`, `status()` spinner
  factory, `GLYPHS`, `supports_ansi()` probe.
- **Task C (loading animations)**: `cli/interactive.py::LiveMonitor` —
  tails `logs/{task_id}/trace.jsonl` (the harness's PUBLIC observability
  surface — no reaching into run_task internals) in a daemon thread while
  run_task blocks; renders a rich Status spinner with live label per event
  kind (baseline verify → retrieval → planning → attempt N → model: thinking
  (step) → sandbox: running command → verifier: running tests → git output)
  + running event count + ACCRUING COST from model_response usage records.
  Used by both `vex fix` and the interactive mode. Survives missing/
  rotating trace files (the harness's archive-then-create path) without
  raising — tested.
- **Task D (interactive natural-language mode — the primary UX)**: `vex`
  with no args + a TTY → banner + plain-language REPL (cli/interactive.py::
  run_interactive). A typed sentence ("fix the login bug...") IS the issue
  text; repo inferred from CWD (`.git`/pyproject/setup.py/requirements
  probe); `repo <path>`/`model <name>` switch mid-session; `status`/
  `diff` re-render the last run; `help`/`exit`/Ctrl+D. Per-fix output:
  live spinner → status line with attempts/model calls/cost/elapsed →
  verification PASS/FAIL chips → colored diff → markdown rationale.md →
  trace path. Non-TTY no-args (pipes/CI) → argparse usage, exit 2, no
  traceback, no hang (tested via real subprocess). Flag commands are
  unchanged in behavior — the scriptable path.
- **Task E (Claude-Code-style features)** — implemented: live cost/token
  ticker (LiveMonitor + result summary), colored diff rendering
  (ui.print_diff: +green/-red/@@muted/file headers gold), interactive
  approval prompts with rendered diff preview (watch_for_approvals —
  renders request.json's diff + issue, prompts, writes decision.json via
  runtime.approval.decide; `vex fix --approval` / benchmark subsets with
  `config.approval="require"` park workers, the watcher prompts inline),
  live multi-task benchmark table (rich Live + Table in a daemon thread;
  per-task running/✔/✘ states; scheduler runs in a thread so the table
  and Ctrl+C stay responsive), markdown rationale rendering
  (rich.markdown.Markdown in fix + interactive + benchmark), graceful
  Ctrl+C (scheduler's own KeyboardInterrupt worker-kill + checkpoint
  preservation, PLUS `_cleanup_after_interrupt()` sweeping THIS process's
  orphaned sandbox containers via execution.sandbox's public reaper —
  tested with mocked sandbox).
  - **Skipped (and why)**: none of the offered features were skipped;
    the list is fully covered. (Deeper TUI — paneled Claude-Code-style
    layout with multiple simultaneous views — considered and skipped:
    rich Live single-widget is the right scope; full TUI frameworks
    (textual) would be a new dependency for marginal gain.)

### Notable implementation details future-you should know

- `_BenchmarkLiveView` uses rich `Live(transient=True)` — the table
  vanishes when done; the final per-task summary list follows (the
  "complementing the web dashboard" requirement — nothing duplicated).
- The scheduler thread wrapper in `_call_scheduler` re-raises
  KeyboardInterrupt from the worker thread after a bounded join, then
  main()'s handler runs the container sweep.
- `--no-color` is accepted at top level AND on every subparser (users
  type it late); `NO_COLOR` env var also honored (rich convention).
- Round 6 hardening fully preserved: safe_task_dir guard, subset type
  validation, null-byte rejections — 62/62 adversarial tests green.
- test_cli.py: 3 assertions updated for the new render format
  ("=== task" → "task fix-", "target test:   PASS" → "target test:" +
  "PASS", "benchmark: smoke" → "smoke"); same verification intent,
  documented for T4's review in the INTERFACES.md Change Log.

## Deferred (Vex pass)
- Remove the `harness` console-script alias once every terminal's docs
  reference `vex` (demo/README.md, demo/run_demo.py, demo/AGENTS.md still
  say `harness fix` — T4's module, left untouched deliberately).
- Tab-completion / shell integration for the interactive prompt
  (readline on POSIX only; deliberate skip for cross-platform parity).

## Vex Side Task 2 (2026-09-09, Terminal 2 session) — session persistence, slash commands, config file, plan preview

*(Continuation of the Vex CLI pass — same visiting Terminal 2 session.
Built AFTER Side Task 1 (interactive mode/theme/animations) was complete,
as instructed. All four additions lean on existing backend capability;
nothing new was built below the CLI layer except the session index file.)*

### Task A — session persistence: DONE (live-verified end to end)

- `vex --continue` — resumes the most recent RESUMABLE session (no
  subcommand needed; handled pre-parse since subparsers are required).
- `vex --list-sessions` — recent runs, resumable ones marked `R` with
  timestamps/status/issue excerpts.
- `vex --resume <task_id>` — resume one by id (usage error without id).
- In-session: `/sessions` lists, `/resume <id>` continues.
- **Session index**: `logs/.vex-sessions.jsonl` (append-only; runs
  recorded on completion AND on interrupt). **Directory-scan fallback**:
  `list_sessions` also scans the log root for resumable task dirs the
  index misses (pre-ST2 runs, killed workers, lost index) — deduped by
  task_id, metadata recovered from each run's own task_start trace event.
- **Resumable** = state.json has completed AND remaining steps AND no
  final `result` event — exactly what the harness's resume contract
  (config["resume"]=True + same task_id) can continue. Pre-step-1
  interruptions are correctly NOT resumable (harness restarts those
  fresh BY DESIGN — runtime/AGENTS.md's documented semantics).
- `_resume_task` rebuilds the Task from the run's OWN task_start event
  (repo/issue/config, api_key stripped) + `config["resume"]=True`, then
  drives the shared executor. Session `model`/`provider` pins override.
- **LIVE E2E (real modules, scripted model)**: child process hard-killed
  itself after step 1 of 2 (T1's test_resume_after_hard_kill pattern,
  rc=70) → session discovered via dir-scan as resumable → `--continue`
  path resumed it: `step_skipped_resume` + `attempt_resume` + finished
  all steps + git output + rationale ("resumed from interrupted run;
  kept surviving work copy"). Same task_id, one continuous trace.
- Ctrl+C delivery note for future-you: verifying "user interrupts a
  run" on Windows from a TEST requires a child process —
  `signal.raise_signal` from a non-main thread is silently dropped, and
  `CTRL_C_EVENT` on a shared console kills your own shell too. Use the
  hard-kill pattern (above) or CREATE_NEW_PROCESS_GROUP + child-delivered
  CTRL_C_EVENT; a REAL user's Ctrl+C reaches the main thread directly
  (product path unaffected).

### Task B — slash commands: DONE

`/help` `/status` (last task's state) `/diff` `/sessions` `/resume
<task_id>` `/approve [id]` `/reject [id]` (write decision.json through
runtime.approval's EXISTING file protocol — no new mechanism) `/cancel`
(SIGINT semantics to the running task: checkpoints kept, resumable —
same as Ctrl+C) `/quiet` (toggle spinner verbosity). Unknown slash
command → hint, never a crash. Bare-word commands (help/repo/model/...)
kept from Side Task 1 alongside.

### Task C — config file: DONE

- `cli/vexconfig.py` — `~/.vex/config.toml` (or `$VEX_CONFIG`): model,
  provider, budget_cap_usd, max_retries, plan_preview, log_verbosity
  (normal|quiet), log_root (for --continue/--list-sessions).
- **Precedence: explicit (flags/session) > file > harness DEFAULTS** —
  `apply_config_defaults` fills only keys the caller didn't set.
  Unknown keys pass through untouched (mirrors harness.get_config
  philosophy — future/other-terminal knobs work without changes here).
- A `[vex]` sub-table is accepted for grouping. Broken TOML / wrong-typed
  values → one-shot stderr warning + ignore (never crash the CLI); no
  TOML parser at all → empty config.
- Interactive session loads it once at startup (model/pins/preview
  defaults); `_run_one_fix` merges it under session state.

### Task D — plan preview: DONE

- `plan_preview = true` in config (or session `/preview` state) → before
  edits start, the preview watcher renders the FIRST `plan` trace event
  (the harness's public decomposition: numbered steps + "done when"
  checkpoints) and prompts: approve → run proceeds; reject → SIGINT
  semantics cancel the run with checkpoints kept.
- Default OFF (fully autonomous) per the "skippable" requirement;
  session state beats config file (explicit > file precedence).
- Reuses Ctrl+C/checkpoint machinery — NOT a new confirmation protocol.
  (The worker-level approval gate for final diffs is unchanged and
  separate; plan preview gates the EDIT PHASE, approval gates the RESULT.)
- Preview cancels itself if the run finishes before an answer.

### Tests (all green)

- NEW tests/test_cli_vex2.py — 24: config (6: flat+table parse, broken
  TOML, wrong types, precedence, unknown passthrough, no-parser
  fallback), sessions (7: record/list, resumable detection incl.
  finished/missing, most-recent, rebuild-with-resume-flag, clean
  missing-run, bare CLI flags, missing-id usage error), slash (6: help
  coverage, unknown hint, no-run guards, approve/reject via the real
  approval protocol, /cancel signal interception, /quiet), preview (4:
  render+accept, reject+SIGINT+resume-hint, early-cancel, config
  flow-through with precedence).
- Full regression: test_cli + test_cli_adversarial + test_cli_vex +
  test_cli_vex2 + scheduler-integration = **126/126**.
- Live E2E above (not a pytest test — spawns a self-killing child).

### Future work (NOT built — ideas that came up, deliberately deferred)

- `/preview` as a live in-session toggle + plan EDITING before approval
  (needs a harness-side re-plan entrypoint; currently preview is
  approve/reject only).
- Session index compaction (logs/.vex-sessions.jsonl grows unboundedly;
  trivial cap-by-age when it ever matters).
- `vex sessions prune` / archiving of old resumable states.
- Config file `profiles` (named preset blocks switchable with
  `--profile`).
- Rich rule-based diff SYNTAX highlighting per-language (current:
  +/-/hunk coloring; full pygments tokens are rich-able but noisy).
- Interactive prompt history search (readline where available).
