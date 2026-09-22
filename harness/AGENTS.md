# AGENTS.md — Terminal 1: Harness Core & Context Management

## Agent demo-parity round (2026-09-21) — resume replays, fetch already live

**`run_agent` gains additive `resume_history` (history replay, not a
restart); new `load_resume_history(task_id, log_root)` rebuilds the
prior-session preamble (request + files touched + recent exchanges)
from the run's own trace.jsonl — never raises, "" when nothing to
replay. The history injects as a steering-context user message
(at=agent-resume-history, same trace kind as plan guidance) so the
loop starts from the current tree with earlier work visible.
Pristine/orig discipline unchanged (snapshot-once, orig-once — a
resumed same-id run keeps both, test-pinned). Fetch/MCP/plugin verbs,
approval_required/decided tracing, and the require-without-approver
honest refusal are unchanged (already built in the trust round).
`vex fix` modules untouched (core/editor/tools/prompts/router/verify/
sandbox byte-identical to this round — no edits there).

### Verification

- tests/test_agent_loop.py: new TestAgentResumeHistory (4 tests:
  replay content, empty-on-missing, steering injection reaches the
  model + trace, same-id pristine/orig kept).
- demo/agent_demo.py drives the loop offline (scripted model, local
  tools only) through question -> mention -> plan -> approval ->
  undo -> resume -> compact, exit 0.

## General agent loop (2026-09-21) — `harness/agent_loop.py`, the interactive engine

**The interactive `vex` session is now a general coding agent; `vex fix`
(the verifier-gated benchmark path through `core.run_task`) is UNCHANGED
— same loop, same verification, same git output. The session used to
dispatch four modes (fix/question/build/research via harness.router);
it now classifies question | agent_task | chit_chat and runs ONE tool
loop for everything work-shaped (fix/build/refactor/run/debug).**

### What's built

- **New module `harness/agent_loop.py`** (only new harness surface):
  - `classify_agent_input` / `classify_deterministic` — deterministic
    rules (run verbs, fix verbs, build verbs, research markers,
    explain/question shapes, chit-chat openers) + ONE cheap model call
    (`difficulty_hint="easy"`) for the gray zone only; any model
    failure degrades to chit_chat with a clarifying reply (never
    launches). `agent_intent_enabled=False` = everything is agent_task.
  - `run_agent(request, repo_path, config, log_root, task_id,
    approve_fn, on_event)` — the loop, on the LIVE repo (edits in
    place; no pristine/work build copies). Tools: READ/GLOB/GREP/BASH
    (via the existing `BashSession` → `execute_sandboxed` boundary,
    deny-guard included)/EDIT (exact-block replace)/WRITE/MEMORY
    (decision_memory query)/VERIFY/DONE. Model protocol: one JSON
    `{"tool": ...}` (fenced or bare) or one plain line per reply;
    unparseable replies get one retry nudge, never a crash.
  - Steering (`harness/steering.py`, reused as-is) polled every turn:
    guide injects into the live messages, abort stops cleanly,
    replan rides as strong guidance (no fixed plan to replace).
  - NO verifier gate by default — DONE mints success; `verify()` runs
    only when `target_test`/`test_command` are set (or on explicit
    VERIFY). Stopping: `agent_max_turns` (25), budget cap, wall-clock.
  - Diff/undo: `logs/{task_id}/pristine/` (snapshot at start, reference
    ONLY) + `orig/` per-file originals stashed before first edit;
    `agent_diff` (unified diff vs live repo) and `undo_edits` (newest-
    first restore; agent-created files deleted on `undo all`).
  - Trace: same public kinds the CLI renders (task_start mode=agent,
    retrieval, decision_memory, model_request/response via ModelClient,
    tool_call/tool_result, verify, steering*, task_end, result) plus
    additive `edit_applied` / `approval_required` / `approval_decided`.
  - Permission model: reads always run; BASH/EDIT/WRITE need
    `approve_fn` approval only when `agent_approval="require"`
    (no approver = honest refusal, never silent).
- **Config keys** (all additive in config.py): `agent_max_turns`,
  `agent_approval`, `agent_context_files`, `agent_context_lines`,
  `agent_max_read_chars`, `agent_intent_enabled`.
- **Untouched**: `core.py`, `editor.py`, `tools.py`, `router.py`,
  `intent.py`, `qa_mode.py`, `build_mode.py`, `research_mode.py` —
  the benchmark path and all mode modules are byte-identical.

### Decisions worth knowing

- Research-shaped input ("research how X compares...") maps to
  **question** (read-only Q&A) — the agent loop has no web FETCH; the
  legacy research entry stays for programmatic callers that need it.
- `/plan <text>` and `/review`-with-template stay on the fix loop in
  the CLI (preview machinery lives in `_execute_task`; the agent loop
  has no preview) — deliberate, documented in cli/AGENTS.md.
- `BashSession` is reused (not raw `execute_sandboxed`) so the deny
  guard, cwd tracking, and output caps behave exactly like the fix
  loop's; traversal-unsafe READ/EDIT/WRITE paths are refused
  harness-side before any I/O.
- Classification order matters: run > fix > research > question-shape
  > build > artifact-declarative > unknown. "Research the crash" is a
  task (fix wins); "what should I add?" is a question (shape wins
  over the build verb).

### Verification

- tests/test_agent_loop.py: **32/32** (classification matrix incl. the
  three dogfood sentences, tool parsing, DONE/READ/EDIT flows on a tmp
  repo with diff+undo, fake-sandbox BASH, verifier on/off, approval
  require allow/refuse, steering abort, traversal refusal, trace file).
- Regression: test_modes 97+2skip, test_steering 35+6skip (Docker-gated
  skips — daemon down machine-wide at round time), test_cli_tui 43/43,
  test_cli_vex3 + tracelog + agent 148, CLI sweep 346+1fix (the 1 was
  the custom-command dispatch update, fixed in-test).
- Docker-down environmental failures (pre-existing suites needing the
  daemon: test_cli fix e2e, release --json): fail at baseline verify
  before any touched code runs; re-run when the daemon is back.
- ruff: harness/agent_loop.py + tests/test_agent_loop.py clean;
  interactive.py/tui.py held at their pre-existing finding sets (all
  flagged lines are pre-existing regions).

### Not yet implemented / honest notes

- Agent resume (`/resume` on an agent task) restarts the loop under
  the same task id from the current tree — message history is not
  replayed, but pristine/orig references are kept so diff/undo stay
  coherent (undo appends an `undo` trace event; per-file
  `/diff undo <file>` and `undo all` with created-file deletion work
  post-resume because orig/ is never wiped).

## Agent trust round (2026-09-21) — approval modal path, plan preview, undo, MCP/plugin/fetch

**The four "Not yet implemented" items from the section above are now
built (only the message-history-replay caveat on resume remains, by
design — the loop starts from the current tree).**

- **Tools**: `fetch` (read-only web, GET-only SSRF-guarded + capped +
  budgeted via `agent_fetch_enabled`/`agent_max_fetches`; BASH never
  shells a FETCH line), `mcp`/`mcp_call` (plugin servers + config
  `agent_mcp_servers`; failures -> TOOL ERROR kinds, never traceback),
  plugin verbs through one `route_tool` (builtin|mcp|plugin|unknown;
  read-only shapes auto-run even under require; unknown tools honest
  errors). `parse_tool_call` takes optional `known_verbs` (default
  strict). BASH runs LIVE (local subprocess, never Docker; injected
  fakes still win; deny-guard + cwd + caps kept; Ctrl+C stops the
  call, never the session).
- **Plan**: `render_agent_plan` (heuristic steps + retrieval files, no
  verifier fabrications); approved plans inject as `plan_guidance`
  steering (replan-path semantics), never a fixed contract.
- **Undo**: per-file `targets=` + `undo all` (created files deleted) +
  `undo` trace event; pristine/ never touched.
- **Config** (all additive): `agent_fetch_enabled`, `agent_max_fetches`,
  `agent_live_bash`, `agent_mcp_servers`.
- **Verification**: tests/test_agent_loop.py 56/56 (plan, fetch incl.
  budget, MCP incl. live-server drive, plugin verbs incl. require-mode
  auto, undo incl. trace event + pristine intact, Ctrl+C BASH,
  REPL preview approve/edit/cancel); live drive
  (Temp/opencode/drive_agent_trust.py): require-mode approve+deny+undo,
  plan->edit->DONE, real `python -m mcp_server` query_decisions through
  the loop; `vex fix` modules untouched (core/editor/tools/prompts/
  router/verify/sandbox); evals --check OK.
- **CLI surfaces** (see cli/AGENTS.md): TUI `_agent_approve_fn` modal
  (y=once/a=always/n), `/plan` agent preview, `/diff undo <file|all>`.

## Mid-Task Interactive Steering round (2026-09-14) — steering a live task (Tasks A-C)

**New user instructions injected while a task runs — "actually, only
touch file X", "stop, that's the wrong approach" — WITHOUT losing the
task's progress.** New module `harness/steering.py`; loop consumption
points wired in `core.py`; REPL reader + registration in `cli/interactive.py`
(see cli/AGENTS.md for the surfaces). One mechanism, three surfaces
(REPL plain-text, TUI plain-text//steer, cross-process journal writes —
a second terminal can inject by appending to the journal).

### The mechanism (Task A)

- **Transport**: `logs/{task_id}/steering.jsonl`, an append-only journal
  (inject + consume records, one JSON object per line). ANY process can
  inject; the loop polls at safe checkpoints. CROSS-INSTANCE
  CORRECTNESS (the round's fatal pre-fix bug): the CLI-side injector
  buffer and the loop's buffer are different objects — every consumer
  poll (`pending`/`take`/`steering_context`/`inject`) RE-SCANS the
  journal first (`refresh()`; binary-mode incremental scan keyed by a
  byte cursor — torn tails retried, shrunk journals rebuilt). Without
  this, steering never reached the running task.
- **Intents are explicit, parsed at INJECT time** (`parse_steering_line`):
  plain text → `guide`; `replan:`/`replan <text>` (or bare `replan`) →
  `replan`; `abort`/`abort:` → `abort`. Unknown/empty → guide (the
  least-destructive intent; a wrong guess must never abort a run).
- **Safe checkpoints (consume points)**, innermost first: (1) TURN
  boundaries in `run_step` — between model calls, guide events inject a
  `USER STEERING` message into the LIVE session (the model continues
  with the instruction visible); strong intents end the step
  (`STEER-REPLAN:`/`STEER-ABORT:` note) so (2) the STEP-boundary handler
  acts: abort → clean resumable stop; replan → the shared re-plan path;
  (3) the FINAL-GATE check before the final verify; (4) the PRE-MINT
  re-check after verify/agent-tests/self-critique all pass but before
  success is minted (closes the window where steering arrives DURING
  those long-running stages).

### State machine interaction (Task B)

- A steering interrupt is NOT a state transition — `machine.record_event`
  ("steering"/"steering_replan"/"steering_abort") appends a non-transition
  audit record (phase unchanged) to transitions.jsonl, so the trail shows
  WHEN the user redirected without ever corrupting a valid edge.
- The two strong intents move the machine through EXISTING forward
  edges only: replan = editing → repairing → planning (`_do_steering_replan`:
  the attempt is dismantled for a new plan; work-in-progress KEPT —
  `keep_work_next`, same exemption shape as a resume's first iteration;
  the attempt slot is given back); abort = `<current>` → `failed` via
  `_steering_abort` (every non-terminal phase allows the failed edge;
  without the transition the live-status phase would stay stale forever).
- The re-plan prompt carries ALL steering (consumed + pending —
  `steering_context`, capped by `steering_max_chars`) PLUS the current
  work diff, and the section sits AFTER `## Retrieved context` so the
  difficulty predictor's first-message cut is unaffected.

### The guarantees (Task C) — all test-pinned

- **Verifier-gated completion is never shortcut**: pending steering at
  the final gate or the pre-mint checkpoint BLOCKS success minting —
  the attempt is poisoned (`success minting deferred` decision) and the
  fix is re-verified after the steering is incorporated. Guide rides
  the next attempt's feedback; replan replaces the plan. Success is
  still only ever minted on a real verifier pass.
- **Checkpoint/resume is intact after steering**: an inject without a
  matching consume is STILL PENDING after a crash (the journal replays
  on buffer construction) — a steered task interrupted after steering
  resumes with the steering intact. A steered abort leaves work/,
  state.json, plan.json, and the journal — resumable via `vex --resume`.
- **The OFF arm is one code path**: `steering_enabled: False` → the
  loop's buffer is None → the journal is never polled (an injected
  abort sits unconsumed; the run finishes as if nothing was typed).

### Config keys (all additive, in config.py)

`steering_enabled` (True — the OFF arm), `max_pending_steering` (16 —
bounded queue; over-cap injects are REFUSED honestly, never silently
dropped), `steering_max_chars` (4000 — re-plan context cap).

### Verification at close

- tests/test_steering.py: **41/41** — parse matrix; buffer unit
  (journal/replay/cap/torn-tail/shrink/thread-safety); CROSS-INSTANCE
  transport incl. a real second-process inject; state-machine
  `record_event` phase-neutrality + replan forward-edges + abort
  landing; `steer_live_run` (ack/journal/convo-refusal/say-hook);
  `_execute_task` registration + crash-clear; Docker-gated e2e through
  the REAL loop: guide at a turn boundary (consumed, fix still
  verified), abort at a step boundary (failed + resumable artifacts +
  machine failed), replan (plan_replaced + work kept + steering in the
  re-plan prompt), final-gate guide (pre-mint block, attempts ≥ 2,
  deferred decision, eventual honest success), OFF arm (journal
  unconsumed, success), resume-after-steering (journal replay).
- TUI behavior pinned in tests/test_cli_tui.py (`/steer` in help;
  in-flight plain text steers + journal write; live-run registration).

### Honest notes / known limits

- A journal replaced in place with same-or-larger content (a pathological
  archive collision) is not detectable by size — the shrink guard covers
  truncation; the real archive path moves the whole directory, so this
  cannot occur in practice (test documents the behavior).
- Steering during the baseline verify (pre-plan) is consumed at the
  first turn boundary of the first step — no earlier consume point
  exists, by design (planning must not be interrupted mid-model-call).
- Steering typed BEFORE the loop starts (a build's stage-1 window) is
  refused honestly by the CLI's live-gate (trace.jsonl must exist —
  see cli/AGENTS.md); the harness never sees it, by design (the
  pre-loop journal would be archived on the loop's fresh start).
- `max_pending_steering` refuses the 17th concurrent pending
  instruction; the user sees an honest "steering refused" ack.

## Proactive Codebase Health Scan round (2026-09-14) — `vex scan` (Tasks A-C)

**A new read-only scan mode — genuinely new autonomous-initiative behavior,
not a reactive fix: without a bug report, analyze a repo and surface what's
actually worth a developer's attention.** New module `harness/scan_mode.py`;
CLI subcommand `vex scan` (cli/main.py, additive); config keys +
`--finding` handoff in `fix`. Read-only BY CONSTRUCTION: no shell, no
sandbox, no editor, no pristine/work copies, and NO model calls — findings
come from the graph + AST + manifests, ranked deterministically from each
finding's own evidence (the rationale-log discipline, no invention).

### Task A — run_scan (the analysis)

- Three detectors: **coverage gaps** (the EXISTING memory.code_graph via
  harness.deps — module-level gaps by test-import/call over-approximation
  plus a one-import-hop indirect-coverage rule that kills the
  helper-module false-positive class the first live run exhibited;
  function-level gaps only for load-bearing symbols ≥2 non-test call
  sites inside otherwise-covered modules), **latent-bug smells** (stdlib
  AST; only the three classes with real failure histories — mutable
  defaults, bare except, swallowed exceptions; style nits deliberately
  out of scope), **dependencies** (pin conflicts across manifests
  offline + opt-in PyPI freshness via `--remote`/`scan_remote_deps`,
  network OFF by default exactly like `docs_lookup_allow_remote`;
  "known issues" NOT claimed — the honest signal is version distance).
- Read-only invariants are TEST-PINNED (tests/test_scan_mode.py
  TestReadOnlyInvariants): no BashSession/execute_sandboxed imports, no
  writes inside the scanned repo, graph index built OUTSIDE the repo.
- Honest degradation: no .py sources → coverage/smell skipped with a
  note; missing graph layer → coverage skipped, never an exception; a
  repo that is not a directory → status "error" with a note.
- Artifacts per scan: `logs/scan-<id>/` with `scan.json` (ALL ranked
  findings, `index`-stamped), `report.md`, `trace.jsonl` (task_start /
  scan_detector×3 / scan_summary / task_end).

### Task B — ranking + rationales (the noise budget)

- Findings scored (severity × kind weight + structural signals: fan-in,
  call-site count) and sorted; the report shows only the top
  `scan_max_findings` (8) — the rest stay in scan.json, resurfaced via
  `vex scan --max-findings N`. **The first live runs found 101–1323
  smell sites and 20+ coverage candidates — the noise budget is what
  makes the output useful**; `scan_smells_per_kind` (3) and
  `scan_func_gap_max` (3) cap per-kind output, preferring
  structurally load-bearing files.
- Every finding carries a 2-4 sentence rationale in the fix-round
  rationale-log style: what was found, why it deserves attention, what
  the first step is — deterministic from the finding's own evidence.
- CLI: `vex scan --repo . [--focus coverage|smells|dependencies]
  [--remote] [--max-findings N] [--json] [--fix N]`; `--fix`/
  `--json` mutually exclusive; `--focus` validates its choices.

### Task C — the handoff (`vex fix --finding <scan_id>#<n>`)

- Every finding carries a fix contract (`fix_kind` / `fix_issue_text`
  / `fix_target_test` / `fix_note`): coverage/smell findings route
  through the plain fix loop with a suggested NOT-YET-EXISTING target
  test (baseline verify fails honestly; the issue text explicitly
  authorizes test-writing, squaring the loop's don't-touch-tests rule);
  dependency bumps route through build mode (a version-floor acceptance
  test genuinely fails on the old pin).
- `resolve_finding` runs the scan id through memory.paths.safe_task_dir
  (traversal-shaped ids rejected before any filesystem use); the index
  is 1-based against the ranked order the report printed.
- E2E-PINNED through the REAL loop (Docker-gated
  TestScanFindingToTaskE2E): `vex scan` notices an untested module →
  `vex fix --finding <id>#1` → cmd_fix → run_task → REAL Docker verify
  → success, with the finding's own test file as the target.
- `vex scan --fix N` closes the loop in one command: scan, pick, hand
  off.

### Validation against a real repo (the "genuinely useful, not noisy" gate)

Final run against THIS repo (`vex scan --repo .`): **7 findings, 7
shown, 0 suppressed, 122.8s, exit 0** — 4 coverage gaps (`cli/ui.py:
err_console()` 20 call sites; `execution/sandbox.py: docker_available()`
10; `memory/paths.py: default_logs_dir()` 12; `cli/uninstall.py`
whole module) + 3 swallowed-exception smells in execution/sandbox.py —
every one spot-checked genuine (no tests exercise those symbols; the
smell lines are real `except: pass` handlers). Iteration history that
shaped the caps: early runs surfaced 1323 raw smell sites / 20+
coverage candidates; per-kind caps + fan-in ordering + the 8-finding
budget reduced that to the 7 worth attention. Report at
`logs/scan-6045bc18/report.md`.

### Config keys (all additive, documented in config.py)

`scan_max_findings` (8), `scan_remote_deps` (False),
`scan_pypi_timeout_s` (10), `scan_smells_per_kind` (3),
`scan_func_gap_max` (3).

### Verification at close

- tests/test_scan_mode.py: **50/50** (run_scan basics; coverage/
  smell/dependency detectors incl. mocked-PyPI remote; ranking +
  suppression; finding resolution incl. hostile refs; CLI scan +
  `--json`; `vex fix --finding` handoff; the Docker-gated e2e; the
  read-only invariants).
- Real-repo run: exit 0, 7 focused findings, spot-checked genuine.

### Honest notes / known limits

- Coverage "exercise" is an import/call over-approximation (the
  coordination fan-out's recall-over-precision trade) — a module
  imported by a tested module counts as indirectly covered (one hop
  only); a function hit ONLY by import side-effect isn't distinguished
  from a real behavior test. Precision could improve with coverage-
  tool integration — deliberately not built (a scan must stay
  instant, offline, read-only; no test execution).
- Detectors are Python-first; a repo with no .py sources reports
  honestly. JS/TS graph indexing exists upstream but the AST smell
  pass does not — documented limitation, not a silent skip.
- The dependency detector scans requirements.txt + [project]
  dependencies; other manifest shapes (setup.py, Poetry/PDM sections)
  are not parsed.

## Long-Horizon Planning round (2026-09-14) — multi-session build projects (Tasks A-C)

**Build mode planned within a single session; this round added the layer
ABOVE it for feature requests too large for one session: a PROJECT PLAN
of smaller, independently-checkpointed sub-tasks that span multiple
sessions — using the existing per-task machinery unchanged, but tracked
at a higher level (completed SUB-TASKS across sessions, not steps within
one).** New module `harness/build_plan.py`; prompts + config keys in
`prompts.py`/`config.py`; router dispatch in `router.py`. Build mode's
basic version (Modes round, Task D) existed first — this built on it, as
instructed.

### Task A — multi-session task decomposition (`run_project`)

Three stages, one or MANY invocations:

1. **Stage 1 — criteria extraction (Task B, see below)**.
2. **Stage 2 — decomposition**: ONE model call
   (`render_project_plan_prompt`) decomposes the feature into ordered
   sub-tasks, each mapped to the criteria ids it delivers. A plan whose
   sub-tasks don't cover every criterion is a HARD planning error (the
   contract can never complete — honest abort, never a partial effort).
3. **Stage 3 — execution**: each sub-task is ONE unchanged
   `build_mode.run_build` session — sub-request = the sub-task's
   description + its criteria sentences; start repo = the ORIGINAL repo
   for sub-task 1, the ACCUMULATED verified tree for later ones. On
   success the sub-task's verified `work/` is PINNED to
   `logs/{project_id}.tree-s<N>/` (a stable sibling — the sub-task's own
   task dir can be archived by a later re-run of the same id; the pinned
   tree must not) and becomes `current_tree` in the project file.

**The multi-session pause**: `project_sub_tasks_per_session` (default 1)
bounds BUILDS per invocation (a sub-task that completes
by-verification — its authored tests already pass — consumes no
budget); reaching the budget writes status `"checkpointed"` and
returns — a LATER session continues with `project_resume=True` + the
same project_id (completed sub-tasks are never re-run; the accumulated
tree carries forward; `sessions` counts invocations). A sub-task FAILURE
also checkpoints (completed set kept) — the next session retries just
that sub-task. A sub-task whose authored tests already pass on its
start tree is COMPLETE-BY-VERIFICATION (`already_exists` from run_build
is treated as done — for sub-task 1 on the original repo it means the
feature exists; for later ones it means an earlier sub-task delivered
those criteria early).

### Task B — acceptance-criteria extraction (the whole-effort contract)

BEFORE decomposition, ONE model call
(`render_project_criteria_prompt`) extracts explicit acceptance
criteria — id'd snake_case, one testable behavior sentence each, capped
by `project_criteria_max` (8). This is the completion contract for the
WHOLE effort: every criterion must be claimed by ≥1 sub-task (planning
gate) AND delivered by a completed one (final gate), and the project's
final gate re-runs a targetless full-suite verify on the accumulated
tree (the whole suite is the gate — per-sub-task regression gates
already ran; this is the project-level confirmation). Empty-reply
flake: ONE retry with a repair nudge, then honest error (the standing
discipline — qa/research/build_tests all do this now).

### The project plan format — `logs/{project_id}/project.json`

```json
{
  "project_id": "proj-x",
  "request_text": "<the original feature request>",
  "repo_path": "<ORIGINAL repo — never mutated>",
  "criteria": [{"id": "csv_export", "description": "..."}],
  "sub_tasks": [{"id": 1, "description": "...", "criteria": ["..."],
                 "files_hint": ["..."]}],
  "completed": [1, 2],          // sub-task ids verified done
  "current_tree": "logs/proj-x.tree-s2",  // next sub-task's start repo
  "sessions": 2,                 // run_project invocations so far
  "status": "active|checkpointed|success|already_exists|failed|error"
}
```

Atomic tmp+replace writes (state.json discipline — a crash can never
leave a partial file; pinned-trees are written BEFORE the project file
records them). Layout: project dir `logs/{pid}/`, per-sub-task builds
`logs/{pid}-s<N>/` (+ `.base` staging copies, the build_mode contract),
pinned trees `logs/{pid}.tree-s<N>` — all harness-owned logs-root space,
original repo untouched throughout (test-pinned).

### Task C — the proof (3 distinct module changes, 2 real sessions)

**Fixture `tests/fixtures/feat02_shop`** (new): a small catalog/cart
package where the request genuinely requires several DISTINCT changes:
receipts (new `shoplib/receipts.py`), loyalty points
(`shoplib/pricing.py`), CSV export (`shoplib/serializers.py`) — three
sub-tasks across three modules, not a single-file toggle.

**The e2e proof** (`tests/test_build_plan_e2e.py`, Docker-gated,
scripted model through the REAL loop + REAL Docker sandbox/verify at
every gate): SESSION 1 extracts criteria, decomposes (3 sub-tasks),
completes sub-task 1 (receipts) and CHECKPOINTS at budget 1 —
project.json on disk, pinned tree holds receipts.py + its acceptance
tests, original repo without them. SESSION 2 resumes
(`project_resume=True`): sub-task 2's base copy demonstrably contains
sub-task 1's receipts module (accumulation proven), completes loyalty +
CSV, and the project finishes — all 3 criteria covered, final
targetless full-suite verify green on the accumulated tree, exactly 3
`project_sub_task_start` events across the whole project (sub-task 1
never re-ran). Resuming a finished project is an honest no-op. Plus:
failed-sub-task retry-from-checkpoint (an impossible acceptance test
fails honestly; a later session retries sub-task 2 with a healthy model
and the project completes) and the coverage-gap planning abort.

### Wiring + config

- `router.py`: a build request with `build_project=True` dispatches to
  `run_project` (late import); otherwise the single-session
  `run_build` path is byte-identical. The CLI/session can set the key
  from config files per the two-tier settings convention (no new flag
  needed — it's a task.config value like every other knob).
- Config keys (all additive, documented in config.py):
  `build_project` (False), `project_max_sub_tasks` (4),
  `project_sub_tasks_per_session` (1), `project_criteria_max` (8),
  `project_resume` (False).
- Trace events (logs/{project_id}/trace.jsonl): `project_start`,
  `project_criteria_extracted|capped|parse_error|empty_reply_retry`,
  `project_plan_generated|capped|parse_error|empty_reply_retry`,
  `project_plan_saved`, `project_sub_task_start|end|already_passing`,
  `project_checkpoint`, `project_final_verify`, `project_end` — all
  additive, safe to surface in dashboards (T4).

### Verification at close (all real, not assumed)

- tests/test_build_plan.py: **20/20 offline** (criteria normalization +
  caps + empty-retry + parse-aborts; decomposition + caps + coverage
  gap; project-file roundtrip/atomicity/tolerance; router wiring both
  paths; config defaults; whole-project already_exists — all-passing
  verdict + no verify on the original repo, mixed build/passing with
  the final verify on the ACCUMULATED tree, resume-as-no-op).
- tests/test_build_plan_e2e.py: **3/3 Docker-gated** (the 2-session
  proof above; failed-sub-task checkpoint+retry; coverage-gap abort).
- Regression: test_modes 99 + test_build_plan 20 + test_config_trace_
  state 12 + test_e2e_run_task 28 = **159 green** (~10 min warm
  Docker); test_skills 27 + test_coordination 31 + evals_tasks 8 +
  cli-vex3+tui 82 green; `python -m evals.run --check` 14/14 OK (this
  round changed NO prompts the eval arms diff — the new prompts are
  project-layer only, orthogonal to the fix-loop arms).
- ruff: all NEW/touched files violation-free (`harness/build_plan.py`,
  `tests/test_build_plan*.py`, `router.py`, `config.py`, `prompts.py`).

### Honest notes / known limits

- The criteria contract is enforced at id level: a sub-task CLAIMS
  criteria ids and its own acceptance tests encode them; the harness
  verifies behavior via those tests per sub-task, plus the project-wide
  full-suite verify. It cannot prove a sub-task's tests were
  COMPREHENSIVE for their criterion — that's the same trust model as
  single-session build mode (agent-authored contract), one level up.
- `already_passing` for a sub-task marks it complete BY VERIFICATION
  and the project CONTINUES (one sub-task's passing evidence speaks
  only for its own criteria — a later sub-task may still have real
  work). Two consequences: by-verification completions don't consume
  the session budget (a verification is not a build — an
  all-already-passing project resolves in ONE session, not N), and
  when EVERY sub-task completes that way the whole-project verdict is
  `already_exists` with NO final verify: the only candidate tree would
  be the ORIGINAL repo, which the sandbox mounts read-write —
  verifying it would break the never-mutate guarantee for a verdict
  the per-sub-task verifier runs already established. A resumed
  `already_exists` project no-ops exactly like a finished `success`.
  (Pinned in TestProjectAlreadyExists: all-passing verdict, mixed
  build+passing with the final verify on the ACCUMULATED tree only,
  and resume-as-no-op.)
- Session budget vs wallclock: budget bounds SUB-TASKS per session, not
  time; a sub-task that times out forwards "timeout" and checkpoints
  (retryable) — the standard task-level semantics, one level up.

## Modes round (2026-09-13/14) — intent router & multi-mode capability (Tasks A-F)

**The scope expansion that makes Vex a general coding agent: one input
pipeline, four work modes. The fix-engine is UNCHANGED — build/question/
research are new ENTRIES around it, never loop variants.** New modules
`harness/intent.py` (Task A), `harness/router.py` (Task B),
`harness/qa_mode.py` (Task C), `harness/build_mode.py` (Task D),
`harness/research_mode.py` (Task E); wiring in `cli/interactive.py`
(session dispatch), `harness/core.py` + `context.py` (additive mode key),
`harness/config.py` + `prompts.py`.

### Task A — intent classification (harness/intent.py)

Two tiers, deliberately:
1. **Deterministic rules first** (offline, free, instant): greetings/
   meta-questions → convo; bug-language → fix; build-verb+object →
   build; research-verb+external-marker → research; question shapes →
   question. `hi` costs nothing and NEVER launches a task (the permanent
   fix of the original "hi"-bug — conversational input has a real path).
2. **ONE cheap model call for the gray zone only**: `classify_with_model`
   with `difficulty_hint="easy"` (adaptive routing picks the cheap tier;
   pin `intent_model` to force). ANY failure (endpoint down, empty,
   unparseable, unknown kind) degrades to `ambiguous` — the session
   ASKS one clarifying question instead of guessing (a wrong task-run
   burns minutes+model budget; a question costs one line).
- `intent_enabled=False` (config) = the OFF arm: everything is fix, the
  legacy pre-modes contract.
- Trace: `intent` {kind, reason, tier} on every classification; `route`
  {kind, reason} on dispatch.

### Task B — the router (harness/router.py)

`route(text, repo_path, config, ...)` — classify, then dispatch: fix →
`core.run_task` (unchanged); question/build/research → their modules;
convo/ambiguous → `ModeResult(status="reply", answer=...)` for the
SESSION to print, nothing launched. Thin dispatcher, NO policy beyond
mode selection; late imports so a broken optional mode never breaks the
import graph; `handlers=` override for tests. `route_kind` is the single
import site for the routing decision (the CLI session uses it).

### Task C — Q&A mode (harness/qa_mode.py)

Read-only, no sandbox, no pristine/work copies, nothing to verify:
retrieval (structural+grep, read-only) + decision memory assemble the
context block; ONE model call (plus bounded `READ <path>` round-trips —
a control signal parsed before command extraction, never executed;
traversal-refusing, capped) answers directly. Empty model reply (the
endpoint's documented reasoning-burn flake) → ONE retry with a repair
nudge, then honest `error` — success is NEVER minted on "".

### Task D — build mode (harness/build_mode.py) — the hard one

No pre-existing failing test exists for a new feature, so the completion
criterion is made REAL: **test-first contract**. Stage 1: ONE model call
(`render_build_tests_prompt`) authors the feature's acceptance tests
(same sanitize as fix-mode agent-tests); they're written into a PRIVATE
base copy of the repo (`{task_id}.base` — a SIBLING of the task dir, see
the real bug below); the baseline verify CONFIRMS they FAIL on the
pristine tree (a passing contract means the feature already exists —
reported honestly as `already_exists`, nothing built). Stage 2: the
UNCHANGED fix-engine runs with `target_test` = the acceptance tests —
baseline/regression/flake/agent-tests/self-critique/git output, all
verifier-gated. Build mode is a different ENTRY, not a different loop.
`state.json` gains the additive `mode` key (omitted for fix tasks; the
six Boundary-4 keys stay a strict prefix).

### Task E — research mode (harness/research_mode.py)

Read-only investigation of an external topic: the model drives
`FETCH <url>` (the proven webfetch reader — GET-only, SSRF-guarded,
capped, audited via `web_fetch` trace events) and `DOCS <target>`
round-trips, then synthesizes (direct answer + grounded key findings +
honest not-found line). No shell, no sandbox, no repo edits (test-pinned:
BashSession/execute_sandboxed absent from the module). Budgets:
`research_max_fetches` (4), `research_max_docs` (4), `research_turns` (8).

### Task F — the proof, and the five REAL defects it caught

**Driver: `logs/modes-round/four_modes_session.py`** — one process, one
config, the four canonical inputs + "hi", REAL cloud model
(z-ai/glm-5.3-free @ tokenrouter), REAL Docker sandbox/verify for fix +
build, REAL web fetches for research. Final run
`20260914-004700`: **19/19 checks PASSED**, $0.050 total, all four
modes verifier- or answer-gated, router distinguishing every input, "hi"
launching nothing. Report: `logs/modes-round/four_modes_report_20260914-004700.json`.

The defects the live session found (all fixed + regression-pinned in
tests/test_modes.py + test_webfetch.py):
1. **build_mode's base copy lived INSIDE `logs/{task_id}/`** — the fix
   loop's `_fresh_paths` archives that dir on every fresh start,
   sweeping the staged repo away before the snapshot (WinError 3, status
   "error"). Fix: stage at `{task_id}.base`, a SIBLING of the task dir.
2. **`library` (singular) never matched the external-marker regex**
   (`librar|libraries` + `\b` boundaries) — the num2words research
   question fell to the gray zone. Fix: `librar(y|ies)`.
3. **"an empty list must raise ValueError" — spec language in a build
   request tripped bug-language (`raised?`)**, misrouting build → fix.
   Fix: raise/throw count as symptoms ONLY in the compound form
   ("raises when/if/on") — a bare contract clause is not a bug report.
4. **Empty-reply flakes minted success**: qa_mode returned
   status="success" with answer="" (the endpoint's reasoning-burn
   window). Fix: ONE retry with a repair nudge (a DIFFERENT ask — the
   same prompt deterministically burns twice), then honest error; same
   discipline now in qa_mode, research_mode, AND build_mode's
   test-authoring call.
5. **Glued FETCH URL crashed the fetch**: a degenerate reply glued
   think-tag prose onto the FETCH line; the old DOTALL `parse_fetch`
   swallowed the tail into the URL → "URL can't contain control
   characters" → the whole research run errored. Fix: the URL is ONE
   token (`[^\s<]+` — `<` is the think-tag glue marker) + URL-charset
   validation; plus **degenerate-fetch salvage** in research_mode: a
   reply with no clean tool line that TRIED to fetch ("FETCH" appears +
   an explicit URL) gets the URL salvaged and executed — an attempted
   fetch must never silently degrade into an ungrounded answer.

### Verification at close

- tests/test_modes.py: **100 tests, 100 passed** (intent deterministic
  matrix + gray-zone/model-tier matrix + router dispatch + qa/research/
  build unit + Docker-gated build e2e + session-loop wiring incl. the
  all-four-modes-one-session test) — plus tests/test_webfetch.py 39/39
  with the glued-URL regression.
- The live Task-F session: 19/19 checks, 4 fetches on the research leg,
  the build leg green through REAL Docker verify against its OWN
  authored tests, the fix leg green through the unchanged loop.
- Known limitation (honest): the gray-zone model tier and the mode
  handlers share the endpoint's health — the reasoning-burn flake is
  retried once everywhere now, but a fully degraded window still fails
  runs honestly (error, never a fake success).

## Plugins & Skills round (2026-09-13) — the skills system (Task A; Tasks B+C are CLI-side, see cli/AGENTS.md)

**Skills: auto-invoked markdown instruction packs, modeled directly on
the SKILL.md pattern this project's own environment uses for its built-in
skills — copied deliberately rather than reinvented.** New module
`harness/skills.py`; the planner now scans available skills' descriptions
before every plan and reads + injects the full SKILL.md of any that
plausibly apply. Skills were on the deferred/stretch list and had never
been built.

### Format + locations (the contract)

- **A skill is a folder containing a `SKILL.md`**: frontmatter `name` +
  `description` (WHEN it applies — the matching signal), body = the
  actual instructions/best-practices. Name falls back to the folder name
  when frontmatter is absent; a frontmatter-only file (no body) is
  skipped.
- **Locations** (all scanned every plan; project wins name collisions —
  the specific beats the general): project `<repo>/.vex/skills/<name>/`
  (committed, team-shared), global `~/.config/vex/skills/<name>/`
  (personal), plugin `~/.config/vex/plugins/<plugin>/skills/<name>/`
  (from installed bundles), plus config `skills_roots` (extra roots;
  tests and the eval pin explicit roots here).
- **Auto-invocation** (not inert storage): `core.run_task`'s planning
  step calls `skills.scan_skills_for_task(...)` — discover, match
  against issue words + retrieval terms + repo path/name segments
  (camelCase-decomposed, task-domain stopwords like fix/bug/test/python
  dropped so generic words never trigger), render, inject as the
  planner prompt's `## Applicable skills` section.

### The cross-module hazard, handled the established way

The section sits **AFTER `## Retrieved context` and BEFORE
`## Constraints`** — the exact placement discipline as decision memory
and coordination fan-out, because T3's difficulty predictor cuts the
planner's first user message at `## Retrieved context`. Skills must
inform planning WITHOUT shifting difficulty scoring. Regression-tested
BOTH ways: placement-order test in tests/test_skills.py + a predictor-
invisibility test (`_issue_text_from` on a skills-carrying prompt —
the scary-words skill body never reaches the issue view).

### Config + trace (all knobs task.config-driven per project convention)

- `skills_enabled` (True — False = the OFF arm, exactly one code path;
  the trace then records `skipped: "skills_enabled=False"` and the
  scan function is never called, test-pinned), `skills_max` (3),
  `skills_max_chars` (2500), `skills_roots` (None = default roots only).
- New trace event `skills` {matched: [names], considered, skipped,
  error, section_chars} — auditable even when nothing applies. Safe to
  surface in dashboards (T4).
- Best-effort BY DESIGN (same contract as decision_memory): missing
  roots/unreadable files/broken scan degrade to "(none matched)" + a
  trace error; planning never dies over a malformed markdown file.

### Matching honesty (same vocabulary as the rest of the stack)

Conservative keyword/camelCase-word overlap — NO embeddings. A skill
with no description still matches on its NAME alone (the brief's
canonical "django-conventions skill for a Django repo" case works even
when the issue never says "django": the repo's own path segments are
task vocabulary — same discipline as decision memory's query building).
Synonym gaps ("average" vs `mean()`) remain future work at the same
embedding seam as retrieval/RECALL.

### Tool-verb extension point (harness/tools.py, feeding Task C)

`extend_batch_verbs(verbs)` lets installed plugins extend the BATCH
read-only allowlist with new READ-ONLY diagnostic commands (e.g.
`ruff check`). Defense-in-depth: verb entries must be plain
word/space/dash tokens, and a FIRST-TOKEN deny set (`rm, sed, python,
git, curl, docker, sudo, ...` — see `_DENY_VERB_TOKENS`) makes a
hostile or malformed manifest entry structurally unable to whitelist
destructive or arbitrary-execution commands. `_batch_readonly_pattern()`
merges extensions INSIDE the base pattern's non-capturing group (an
extended verb anchors exactly like a built-in); the forbidden-
composition guard (pipes/redirects/&&/$()) still applies verbatim.
Regression-pinned: `validate_batch(["rm -rf /"])` rejects even when
"rm -rf" was fed as a plugin verb.

### Verification at close (all real, not assumed)

- tests/test_skills.py: **27/27** — parsing (frontmatter/fallbacks/
  malformed/huge-body cap), discovery (precedence, dot-dirs, missing
  roots), matching (django applies to django tasks; pandas does NOT;
  irrelevant skills ignored; repo-name-only match; max bounds), prompt
  placement + predictor invisibility, config defaults, and **three
  REAL-loop e2e** (scripted model through real run_task): content-
  receipt (a matching skill's unique body marker demonstrably arrives
  in the planner's OWN user message), irrelevant-skill non-injection
  (the marker must NOT appear), and OFF-arm (factory-call counter = 0
  when skills_enabled=False).
- Live plugin-chain e2e (driver at Temp/opencode/plugin_e2e.py): install
  the example plugin → apply_tool_extensions → run a real fix on a
  django-shaped fixture → the plugin's skill body marker IS in the
  planner prompt + skills trace event shows the plugin-origin match →
  verified loop SUCCESS.
- **Full eval matrix 14 tasks × 8 arms: 112/112 CLEAN, 0 regressions**
  (the standard pre-ship gate — this round changed the planner prompt).
  New scenario `eval_skills_injection` (genuinely-matching pytest-
  conventions skill via config skills_roots; the scripted fix never
  depends on skill content, so both arms stay deterministic) + new
  `no_skills` arm + `skills_enabled` in `_ROUND_KEYS`. Task set 13→14,
  arms 7→8 — prior reports stay comparable per-task.
- Regression sweep: skills 27 + CLI plugin/command suites 30 + the
  harness/CLI/eval selections — **515 green** across the round's sweep
  (e2e_run_task, decision_memory, coordination, agent_tests,
  self_critique, batch_docs_lint, evals_tasks, adversarial, cli*).
- ruff: all NEW files violation-free; the ratchet shows NO new debt on
  any file this round touched (core.py's count went DOWN 3→2; the
  ratchet's red files are parallel sessions' in-flight work, not this
  round's).

### Example skills shipped (real, not toys)

`tests/fixtures/skills/` — `pytest-conventions` (suite invocation,
node ids, fix-code-not-tests), `django-conventions` (migrations, ORM,
serializers, N+1), `pandas-vectorization` (vectorize loops, NaN
semantics, index alignment, chained assignment). The eval's skills
scenario pins this same root via skills_roots.

## Web-page reading round (2026-09-13) — the FETCH tool (Tasks A+B+C)

**Generalizes and supersedes the narrower docs-lookup scope: a proper,
general-purpose web-fetch capability usable during planning or repair —
not just library docs.** New module `harness/webfetch.py` + a `FETCH
<url>` step-session control signal, wired exactly like RECALL/BATCH/
DOCS (parsed raw AND fence-stripped, never executed as shell).

### Task A — the fetch_webpage tool (harness/webfetch.py, stdlib-only)

- `FETCH <full url>` in place of a bash command → the harness GETs the
  page, extracts readable text, re-injects into the live session.
- **Readability-style extraction** (deliberately simple, per brief):
  a stdlib `HTMLParser` subclass drops script/style/noscript/template/
  svg/iframe/form/nav/header/footer/aside WITH their content; block
  tags give line structure; whitespace/blank-lines collapse; entities
  decode. **Semantic-container preference**: text inside
  `<main>`/`<article>`/PyPI-style `div.project-description` is captured
  per container and the INNERMOST non-empty container wins over the
  whole body (PyPI: the description div beats its `<main>` wrapper, so
  the sidebar/nav never reaches context). Malformed markup tolerated
  (half-parsed text still renders). A JS-only page honestly returns
  `no_text`.
- `parse_fetch` requires an explicit `http(s)://` scheme — `FETCH_ME`
  identifiers never false-trigger (bare words stay bash commands).

### Task B — scope & safety (read-only by construction)

- **GET only**: `urllib.request.Request(method="GET")`, UA declared, no
  body/headers/forms/auth/POST — impossible by construction, not by
  policy.
- **SSRF guard** (the fetch runs on the HOST, outside the sandbox):
  scheme allowlist (http/https) + host blocklist (localhost/loopback/
  Link-Local/Unique-Local/Reserved/Unspecified/multicast IPv4+IPv6
  literals, `0.0.0.0`) — a blocked URL fails CLOSED with a reason slug
  and NEVER opens a socket (regression-tested with urlopen monkeypatched
  to explode). Redirects re-validated at EVERY hop, capped (default 3,
  hard ceil 5), loop-detection via a seen-set.
- **Bounds**: `webfetch_timeout_s` (15, floor 3) bounds the whole fetch;
  `webfetch_max_bytes` (1 MiB, floor 64 KiB) enforced DURING the read
  loop (a huge page can never blow memory, let alone context);
  `webfetch_max_chars` (3000) caps the rendered text with a truncation
  marker; a Content-Type gate refuses non-text/html bodies.
- **Auditability**: every fetch logs a `web_fetch` trace event
  {step_id, turn, url, ok, status, chars} — URL + timestamp + outcome,
  same discipline as any tool call — AND emits to the unified
  cross-module stream (`shared.tracing`, module="harness", opt-in env).
- **Budget**: `max_fetches_per_step` (3) with an exhaustion nudge that
  cannot deadlock (same pattern as RECALL/DOCS); `web_fetch_enabled`
  (True) is the OFF arm (single code path, config-only — the eval's
  `no_webfetch` arm and `pre_round` toggle it).

### Task C — the real case, end-to-end (tests/test_webfetch.py)

**Fixture `tests/fixtures/bug07_num2words`** (new): `year_phrase(2023)`
returns "two thousand and twenty-three" instead of "twenty twenty-three"
— the fix is num2words' dedicated `to="year"` converter, and the
information gap is GENUINE by construction: num2words is NOT installed
on the host (pydoc/DOCS genuinely miss — regression-tested:
`test_docs_genuinely_misses_num2words`), the kwarg is not in the repo,
and a plausible wrong guess (`year=True`) raises TypeError (the honest
control: attempt 1 with the wrong kwarg genuinely FAILS, proving the
API knowledge gap is real — `test_task_c_fixture_genuinely_fails_...`).

**The proof** (`test_task_c_fetch_docs_fixes_unfamiliar_library`,
Docker+network gated): the scripted model FETCHes
`https://pypi.org/project/num2words/` mid-step; the model REFUSES to
apply the fix until `"to: The converter to use"` demonstrably arrives
in ITS OWN message list (content-receipt gate); the applied fix uses
`to="year"`; verified success through the REAL loop + REAL Docker
sandbox/verify; trace shows the `web_fetch` event (url/ok/status) and
NO tool_call ever executed the FETCH line as shell. The honest control
runs the same fixture with FETCH disabled: the wrong guess burns an
attempt before the right kwarg passes — without the web, attempt 1 is
the only shape a guessing model has.

**Live extraction pinned**: `test_live_fetch_pypi_num2words` asserts the
real PyPI page's `to=` converter list arrives, site chrome ("Skip to
main content") does not, and the text is capped.

### Wiring (all additive; no boundary changes)

- `core.run_step`: FETCH intercept after the DOCS block (raw +
  fence-stripped parse), budget counters, trace + unified-stream audit
  closure via `fetch_and_render(audit_hook=...)` (webfetch module stays
  trace-plumbing-free; the loop owns logging).
- `prompts.py`: `## Reading a web page (FETCH)` block in the STEP system
  prompt (planner prompt + difficulty markers untouched — T3's
  predictor cut is safe by the same placement discipline as BATCH/DOCS).
- `config.py`: web_fetch_enabled, max_fetches_per_step,
  webfetch_timeout_s, webfetch_max_bytes, webfetch_max_chars,
  webfetch_max_redirects.
- **evals**: new arm `no_webfetch` + `web_fetch_enabled` in `_ROUND_KEYS`
  (arm-key sync test-pinned); new scenario task `eval_fetch_webpage`
  (13 tasks total now) — the scripted model FETCHes the real PyPI page,
  then fixes a string-to-int bug whose correctness does not depend on
  the fetched CONTENT (determinism preserved on any fetch outcome; the
  scenario guards loop machinery, like eval_docs_lookup).
- New trace events: `web_fetch` (safe to surface in dashboards).

### Verification at close (all real, not assumed)

- tests/test_webfetch.py: **38/38** (35 offline incl. SSRF matrix,
  extraction, bounds, loop wiring e2e via monkeypatched fetcher; 3
  Docker+net gated: the Task C proof, the honest control, DOCS-miss).
- Full eval matrix **13 tasks × 7 arms: 91/91 ok, CLEAN, 0
  regressions** (the standard pre-ship gate for the step-prompt change).
- `python -m evals.run --check`: 13/13 OK (incl. eval_fetch_webpage).
- Harness selection (config_trace_state, stubs_and_deps,
  retrieval_tools, editor_prompts, recall_unit, adversarial,
  tool_errors, batch_docs_lint, webfetch, evals_tasks): **225/225**;
  e2e_run_task **28/28**; coordination/decision_memory/agent_tests/
  self_critique/state_machine **95/95**.
- ruff: new files violation-free; lint ratchet: no new debt from this
  round (the listed failures are other terminals' pre-existing debt).

### Known limitations (honest)

- The readability extractor is deliberately simple (container-preference
  + boilerplate dropping, NOT a scoring algorithm) — pages without
  semantic markup return their whole-body text minus nav/script/footer.
- The unified-tracing emit imports `shared.tracing` lazily inside the
  audit closure (opt-in env; a failure is swallowed — observability
  must never change task outcomes).
- A parallel session's bulk commit `41423dc` swept this round's files
  in mid-flight (verified intact by the green suites above); my
  post-commit lint fixes are the remaining working-tree delta.


## Improvement Round 2, second session (2026-09-11/12) — Agent-written edge-case tests (Tasks A+B+C) + self-critique restoration

(Session interrupted twice: the original terminal closed mid-round —
resumed by forensically reconstructing state from the tree, the empty
`logs/agent-tests-ablation/` dir (created 14:30, never populated), and
the recovery-stash tags; a second interruption closed the terminal during
the fixtures ablation, which continued detached and completed.)

### Pre-flight: a blocked dependency found and fixed first

`tests/test_self_critique.py` failed at COLLECTION — the Agent
Intelligence round's self-critique code (`render_self_critique_prompt`,
`_self_critique`, config keys, Boundary-7 `structured_feedback` on
VerificationResult) was destroyed by the documented git-hook incident
(INTERFACES.md's recovery note: "harness/retrieval.py,
shared/types.py (structured_feedback) and execution/verify.py Round-8
deltas were NOT recoverable from git — T1/T2 please re-write"; the
03:23 snapshot survived only under stash tag `recovery-stash-t1-agent-
intel` with the warning that T1's in-flight re-writes landed after).
This round BUILDS ON that work per its brief, so it was restored FIRST,
from the stash + the surviving tests as the spec:

- `harness/prompts.py`: `SELF_CRITIQUE_SYSTEM` + `render_self_critique_prompt`
- `harness/core.py`: `_self_critique` + the success-path gate (after
  final verify passes, ONE review call of the diff vs the ORIGINAL
  issue; "no" verdict poisons the attempt with the reason as feedback;
  unparseable reply = approve — critique never kills a verified fix on
  its own parse failure) + `_structured_or_tail` feeding
  `_target_feedback`/`_regression_feedback` (Boundary-7 objects when
  present, raw tail otherwise)
- `harness/config.py`: `self_critique` (True), `self_critique_max_chars`
  (8000)
- `shared/types.py`: `structured_feedback: List[Dict] = field(
  default_factory=list)` (additive, default-constructed — every existing
  call site stays signature-valid); `runtime/serialize.py` (T3's file,
  flagged in the Change Log) round-trips it with an absent-key default
  so old journals replay
- 13/13 tests/test_self_critique.py green post-restore (Docker-gated
  e2e included).

### Task A — agent-written edge-case tests (harness/agent_tests.py + prompts + wiring)

The gap: success was gated on the ONE given failing test + full-suite
regression. A fix can satisfy exactly the given test while being wrong
for cases the issue clearly implies (boundaries, error conditions,
adjacent inputs). The gate: after final verify passes but BEFORE
success is minted, ONE model call (`render_agent_tests_prompt` carries
issue + candidate diff + a listing of the repo's existing test files)
returns JSON `{"tests": [{filename, content}]}`. Strict sanitize
(bare `*.py` names only — charset `[A-Za-z0-9._-]`, no path/drive
tricks, dupes dropped, content compiled as Python, config-capped file
count + total chars); anything failing sanitize is DROPPED, never
fatal. Config: `agent_tests` (True), `agent_tests_max` (3),
`agent_tests_dir` ("tests/_agent_generated"), `agent_tests_max_chars`
(12000).

### Task B — same verification rigor, no lighter path

Surviving tests run through the SAME `verify()` everything else uses,
in two stages, on TRANSIENT trees under `logs/{task_id}/agent_tests/`
(never inside work/ — no cleanup can leak into a diff):
1. **Baseline**: `verify(pristine+tests, rerun_for_flake_check=0)` —
   exactly the task-baseline convention. A generated test that PASSES
   pre-fix probes nothing the final gate didn't cover; dropped with a
   trace event.
2. **Post-fix**: `verify(work+tests, rerun_for_flake_check=baseline_reruns)`
   — the generated file is the TARGET: flake-rerun semantics + the
   full-suite regression, identical rigor to the final gate.

Policy (symmetric with the lint gate): a post-fix FAILURE poisons the
attempt (retry with the failing test's output as feedback — structured
Boundary-7 objects when present); a GENERATION problem (model crash,
unparseable reply, zero survivors, copy/verify crash) SKIPS the gate —
never overturn a verified fix over test-writing quality; every skip is
trace-logged. Surviving tests saved to `agent_tests/saved/` for human
review regardless of outcome. Gate order in run_task: edit-validation →
lint → final verify → agent-tests → self-critique → success.

**18/18 tests/test_agent_tests.py green** (parse/sanitize matrix +
Docker-gated e2e: gate-pass with saved-test + work/-hygiene asserts,
gate-poison→retry→pass, unparseable-skip, all-baseline-pass skip,
disabled-config, poison-exhaustion→failed). Full module selection at
close: **181/181** (150 prior + 13 restored self-critique + 18 new),
~6 min warm Docker.

### Task C — the ablation, run honestly (three runs, one real)

`harness/ablation_agenttests.py` (mirrors ablation_memplan's design:
real scheduler → real workers → real run_task → real Docker verify;
model PINNED to glm-5.3 for every call, routing OFF, `self_critique`
pinned OFF in both arms so the agent-tests gate is the only delta;
gate-specific metrics mined from traces: generated/fires/passes/skips).

- **multirepo run (logs/agent-tests-ablation/multirepo)**: DEGENERATE —
  0/5 success BOTH arms. The endpoint's planner calls ran 239–797s and
  every task wallclock-died mid-plan (10 timeouts); the gate never
  reached. Measures endpoint latency, not the mechanism; kept as run-
  condition evidence, quoted as such.
- **fixtures run 1 (fixtures-broken-cap/)**: found a REAL failure mode
  first: the `max_completion_tokens=4000` mitigation (added for the
  documented reasoning-burn hazard) made the generation call return
  EMPTY 5/5 (`raw: ""`, finish_reason=length, 4000 hidden reasoning
  tokens, 0 visible) — probe-verified directly against litellm
  (probe_logs/atgate-mct-*.json): uncapped = 29k hidden tokens then
  content; capped-4000 = empty. The gate's skip policy held (5/5 clean
  skips, zero false poisons) — the harness did its job on bad model
  output. Mitigation REMOVED; wallclock raised to 2400s.
- **fixtures run 2 (logs/agent-tests-ablation/fixtures/, the real one)**:

| arm | success | attempts | calls | tokens | cost | wall |
|---|---|---|---|---|---|---|
| OFF (gate off) | 5/5 100% | 5 | 20 | 48,364 | $0.0557 | 524s |
| ON (gate on) | 3/5 60% | 7 | 33 | 79,941 | $0.1096 | 5677s |

Gate engagement (ON): 8 tests generated across 5 tasks, 3 gates ran to
completion (bug02: 2 tests generated → both survived baseline → both
passed post-fix, gate-pass; bug03/bug05 similar), **0 gate FIRES** (no
fix passed the whole suite while failing an issue-implied edge), 0
skips. The 2 ON-arm failures (bug01, bug04) are wallclock TIMEOUTS
during the gate's generation call — traces show final verify PASSED,
then the ~300s+ generation call blew the 2400s budget (these fixtures'
fixes were verified-good; the success just never got minted before the
kill; resumed once and timed out again in the same place — the
documented slow-endpoint window, 150–345s/call measured in the probes).

**Honest verdict: on this task set the gate did NOT measurably improve
fix quality — 0 fires in 3 completed gates; its measured effect was
purely cost (+$0.054, +65% calls, +3.9x wall on completed tasks) plus
2 successes LOST to the generation call's latency inside a finite
wallclock.** The counter-evidence to "useless": (a) bug02's generated
tests genuinely probed issue-implied edges the 5-test suite didn't
cover (single-element/negative/float mixes) and the correct fix passed
them — a wrong-but-suite-green fix would have been caught (the e2e
poison test proves that path works end-to-end); (b) the 5 fixture
repos have TINY suites (2-5 tests) whose given tests already encode the
edges — the class the gate catches needs a suite whose given test
under-constrains the issue (the multirepo set, where the suite pins
are SUBSET pins — exactly where the gate has room to fire, and exactly
where this endpoint window could not complete a run). Where it would
pay: repos with sparse suites + fast endpoints; with a fast/cheap
generation model (the call is one-shot, no conversation) the latency
cost mostly vanishes. Recommendation recorded: keep the gate ON by
default (cheap insurance when the endpoint is healthy; correct skip
behavior when it isn't), pin `max_wallclock_s` ≥ 2x the observed p95
model call when the gate is on, and re-run the multirepo arm in a
faster endpoint window before making any keep/drop call at scale.

Standing honesty notes: 5 tasks x 1 rep; proxy prices (free-tier
endpoint); the two endpoint-degraded runs are evidence about the
ENDPOINT, not the mechanism; n too small for any success-rate claim in
either direction.

### Files this session

- `harness/agent_tests.py` (NEW), `harness/ablation_agenttests.py` (NEW)
- `harness/core.py` (gate wiring + restored critique + _structured_or_tail),
  `harness/prompts.py` (AGENT_TESTS prompts + restored critique prompt),
  `harness/config.py` (agent_tests* keys + restored critique keys)
- `shared/types.py` (structured_feedback restored), `runtime/serialize.py`
  (round-trip, T3's file, flagged), `INTERFACES.md` (Change Log entry)
- `tests/test_agent_tests.py` (18, was in-tree untracked — verified),
  `tests/test_self_critique.py` (13, passes post-restore)
- probe_logs/atgate-* (endpoint probes: the 29k-hidden-token measurement,
  the cap-vs-empty proof, run logs)

## Interactive-mode fix (2026-09-12, visiting CLI session) — editor.snapshot dst-inside-src RecursionError

**Found live by driving the real `vex` no-args interactive session in
a scratch repo** (the Task-D verification round; full story in
cli/AGENTS.md): `cd <repo>; vex` defaults the log root to ./logs
INSIDE the target repo, so `editor.snapshot(repo, logs/{task_id}/
pristine)` had dst-inside-src and `shutil.copytree` recursed into its
own output until RecursionError → task "error" before the first model
call. Every scripted caller placed logs outside the repo, so the shape
was never exercised.

**Fix (this module's editor.py::snapshot only)**: when dst's parent
chain runs through src, the top chain segment (e.g. `logs`) is
excluded from the copy via the ignore hook. Signature unchanged;
log-root-outside-repo behavior byte-identical (a `logs/` dir that is
real repo content still copies — pinned). Harness artifacts never
belong in the pristine reference anyway, so the exclusion is also
semantically right. 4 regression tests in tests/test_editor_prompts.py
(inside-repo root, outside-root no-over-exclusion, dst-directly-under-
src, plain shape) — file now 19/19; full sweep after the fix: 340
green across the harness + CLI selections (e2e_run_task 28, adversarial
43, coordination 31+5, config_trace_state 12, stubs/retrieval/recall
51, editor_prompts 19, CLI suites 125, coordination_e2e/decision-
memory/env-snapshot 32).

**Also fixed in passing (NOT this module's author, flagged per
cross-terminal practice)**: `harness/ablation_agenttests.py` — a
parallel session's in-flight untracked file — had a PowerShell UTF-8
BOM that tripped test_harness_modules_import_and_parse; stripped the
3 BOM bytes, content untouched (that file is 12/12 after).

## Round 8 (2026-09-10) — agent execution layer & tooling (Tasks A-D)

**54 new tests (all green): tests/test_tool_errors.py (26) +
tests/test_batch_docs_lint.py (28). Full-module selection at close:
204 passed (150 prior + 54 new), ~9 min warm Docker, ~4 min Docker-less
(the new suites need NO Docker).**

### The incident this round survived (read before touching harness files)

At ~09:37 a PARALLEL session ran `git reset --hard HEAD~1` to revert its
own smoke-test commit — which also destroyed every UNCOMMITTED harness
change in the tree: the documented Round-6 adversarial hardening
(tools.py deny patterns, core.py final-edit re-validation, editor.py
traversal normalization), my in-flight Round-8 wiring, and a second
session's in-flight state-machine/self-critique/rerank work. Untracked
files (all new modules + test files) survived. I recovered the Round-6
hardening from dangling stash-commit `fa75dcb` ("round7-verify-
committed-state"), re-applied my Round-8 work on top, and re-verified
(test_adversarial 43/43 green again). tools.py was clobbered twice more
by background git operations mid-round; the recovery pattern that worked:
immediate post-write backups OUTSIDE the repo (Temp/opencode) + import-
verification before each test run. **Lesson for all terminals: COMMIT
(or at minimum back up outside the tree) after every verified-green
milestone — the working tree is shared by 4 sessions and is not safe
storage.** The other session's state-machine/self-critique/rerank/
decision-memory work re-landed from their own session and now coexists
with mine in core.py/context.py/config.py.

### Task A — structured tool-call error handling (harness/tool_errors.py)

A failing/malformed tool call used to feed the model raw exit codes +
stderr dialect noise (or a raised traceback). Now every failure is
classified into a short, actionable error type — `TOOL ERROR [<kind>]:
<detail>` + a one-line `Suggested fix:` — so the model gets a signal to
act on instead of noise to parse. The raw output still rides the result
(capped) for diagnosis; classify() NEVER raises (degrades to
internal_error with the original preserved).

**10 error classes** (26 tests; the requirement was 5):
file_not_found, command_not_found, malformed_patch (with "hunk doesn't
apply at line N"), syntax_error, import_error, undefined_name,
permission_denied (incl. the protected-path shape), timeout,
argument_error, command_rejected (the deny-guard PermissionError), +
internal_error fallback + "ok" no-op so callers can classify
unconditionally.

Wired at three points:
1. `BashSession._map_result` — every nonzero/timeout result gets the
   classification prefixed; exit=0 renders EXACTLY as before (the
   "exit=0" string consumers are unaffected).
2. `BashSession.run` — sandbox-layer exceptions (other than
   PermissionError/SandboxUnavailableError, which keep their own
   classes — fail-loud is unchanged) are classified and re-raised as
   `ToolExecutionError(kind, detail)`.
3. `core.run_step` — catches ToolExecutionError and feeds structured
   feedback (`tool_error` trace event) instead of a traceback.

Two real classifier bugs were found by its own tests and fixed:
patch "at line N" reported the HUNK number not the line; python
SyntaxError line numbers live on the preceding `File "x", line N` line.

### Task B — batched read-only execution (BATCH protocol)

`BATCH <cmd> ;;; <cmd>` — several independent READ-ONLY commands in one
turn instead of one per turn. Deliberately NARROW (per the brief: no
general parallel execution):
- Strict verb allowlist (cat/head/tail/ls/dir/find/grep/rg/wc/file/stat/
  pwd/which/where/env/git status|diff|log|show|blame|ls-files/
  python -m pydoc) + FORBIDDEN composition chars (< > | & ; ` $( and
  newlines). One bad entry rejects the WHOLE batch (all-or-nothing —
  no partial semantics); the rejection names the entry.
- `python -c` is deliberately NOT allowlisted (executes arbitrary code;
  cannot be verified read-only); `python -m pydoc` IS (renders docs only).
- Entries run via ThreadPoolExecutor(4) through the SAME BashSession
  path (deny guard, capping) with throwaway sessions (order-independent:
  no shared cwd coupling). Fenced batches are intercepted on raw AND
  fence-stripped forms (never executed as one shell line — regression-
  tested, same discipline as RECALL).
- Trace: one `batch_call` event (+ per-entry tool_call/tool_result
  flagged `batch: true`, plus `batch_rejected` on refusals).

**Measured wall-clock saving (real Docker sandbox, 8 read-only ops —
the multi-file diagnosis shape)**: read phase 12.62s serial → 6.41s
batched (**1.97× faster, 6.22s saved**; bounded by 4 workers on 8
ops). Full task incl. identical verify overhead: 25.19s → 18.84s
(1.34×). Plus 8 model round-trips collapsed to 2 — with a real model
the saving is strictly larger. Scripts: measure_batch.py (full task)
+ measure_batch_phase.py (phase-isolated); regression-encoding:
test_run_batch_concurrent_wallclock_speedup asserts the concurrency
win in CI (no Docker needed).

### Task C — lint/static-analysis gate (harness/lint.py)

Stdlib-only (no new deps; works offline, every CI cell): per-changed-
file `compile()` syntax check + a conservative single-file undefined-
name pass (module-level uses vs every definition anywhere in the file —
imports incl. star, def/class at any nesting, comprehension targets,
global/nonlocal, walrus, except-as, `__all__` re-exports, builtins).
FALSE-NEGATIVE biased by design (function bodies not analyzed — local
control flow makes single-file analysis false-positive-prone, and the
cost model is asymmetric: a missed error costs one verify which still
catches it; a false positive would block a verifiably-correct fix).
Verified clean against all 5 fixtures + every scripted e2e fix shape.

Wired at TWO points in the loop controller (config: `lint_gate` True,
`lint_names` True):
1. **SUBMIT-time, in-session** (run_step): findings feed back into the
   SAME session (model is mid-context — cheapest fix loop; turn budget
   bounds it). Never ends the step, never gates success.
2. **Pre-final-verify short-circuit** (run_task): between the Round-6
   edit-policy gate and the final verify — a lint failure poisons the
   attempt (retry with findings as feedback) BEFORE burning a sandboxed
   pytest cycle on an edit the AST pass already knew was broken.
   Trace-proven ordering in the regression test: lint_failed →
   attempt_start with NO final_verify between.
Verifier-gated completion stays absolute (spec item 17): lint only
short-circuits what it KNOWS is broken; `lint_failed` trace events
carry the classified findings.

### Task D — documentation/API lookup (DOCS protocol)

`DOCS <dotted target> [topic words]` — same control-signal contract as
RECALL (parsed raw + fence-stripped, never executed as shell). Read-
only by construction; the ONLY remote call is the fixed PyPI JSON
endpoint, GET-only, gated OFF by default (`docs_lookup_allow_remote`
False — network stays opt-in per task).

Resolution layers: shared cache (`logs/_docs-cache/`, same convention
as `_code-graph/` — harness-owned, outside the repo, atomic tmp+replace
writes) → pydoc of the installed interpreter in a SUBPROCESS (never
import agent-adjacent code in-process; a subprocess dies cleanly) →
opt-in PyPI metadata. Caps everywhere (3000 chars default, 10s
timeout). Budget: `max_docs_per_step` (3) with an exhaustion nudge
that can't deadlock (same pattern as RECALL). New trace event
`docs_lookup` {query, source, ok}; pydoc-miss detection is case-
insensitive (a real bug found by its own test).

### Files this round

| File | Role |
|---|---|
| `tool_errors.py` (NEW) | classify()/classify_exception()/render_error(): 10 stable error kinds + hints; never raises |
| `lint.py` (NEW) | lint_file/lint_changed/render_findings: syntax + module-level undefined names, stdlib-only |
| `docs_lookup.py` (NEW) | parse_docs/lookup/lookup_and_render: cache → pydoc subprocess → opt-in PyPI |
| `tools.py` | + parse_docs/parse_batch/validate_batch/run_batch; _map_result classifies failures; run() wraps sandbox exceptions as ToolExecutionError; SandboxUnavailableError still fail-loud |
| `core.py` | + BATCH/DOCS intercepts in run_step (raw + fence-stripped), SUBMIT-time lint gate, pre-final-verify lint short-circuit, tool_error handling |
| `prompts.py` | + BATCH and DOCS doc blocks in the STEP system prompt (planner prompt markers untouched) |
| `config.py` | + lint_gate, lint_names, docs_lookup_enabled, docs_lookup_allow_remote, max_docs_per_step, docs_max_chars |

### Notes for other terminals

- **T3 (runtime)**: state.json schema unchanged; new trace events
  (batch_call, batch_rejected, tool_error, lint_failed, docs_lookup)
  are additive and safe to surface. `cfg["_docs_cache_root"]` is a
  private harness key (underscore prefix) — don't rely on it.
- **T2 (execution)**: nothing needed; all new machinery rides the
  existing execute_sandboxed contract.
- **T4 (memory)**: `logs/_docs-cache/` is shared/harness-owned like
  `_code-graph/` — harmless to prune (rebuilds lazily).
- tests/test_e2e_run_task.py `_assert_logs_complete` now asserts the
  6 Boundary-4 keys as a PREFIX (additive keys allowed) — updated for
  the parallel session's repo_path state key per the INTERFACES.md
  consumer note.

## Improvement Round 2 — memory-informed planning (2026-09-10, T1+T4 joint)

(Owns: harness/prompts.py planner step + the memory module's query
interface. Two OTHER parallel sessions were actively editing this tree
during the round — Round-8 BATCH/DOCS/lint/agent-tests and the
coordinated-changes round below; this round's edits were confined to
prompts.py (planner prompt), core.py (planner wiring), config.py,
context.py (additive state key), deps.py, decision_memory.py (new), and
the memory module's decision_store.py — no overlap with their sections
of prompts.py/core.py beyond additive coexistence, verified by their
suites staying green.)

### Task A — the planner now actively queries decision memory

**The gap was real**: the memory layer stored decisions and was
queryable (MCP `query_decisions`), but the PLANNING STEP never consumed
it — decisions landed in the store only after tasks finished. This was
explicitly listed in "What's next (Phase 3 hooks)" as unstarted.

The wiring, end to end:
- **New module `harness/decision_memory.py`**: `query_planning_decisions
  (repo_path, issue_text, retrieval_terms, limit)` — builds a keyword
  query from the issue's retrieval terms PLUS the repo's own path/name
  segments (convention rows mention the package name; the issue often
  doesn't), calls `DecisionStore.search(query, limit, repo_path=...)`
  scoped to THIS repo, renders text+origin lines. `render_memory_block`
  caps the section (`memory_max_chars`, 1500) with a truncation marker;
  empty renders as an explicit "(none recorded yet)" so the model knows
  memory was CONSULTED and had nothing. Never raises: missing memory
  module / unopenable store / broken query → empty + error string
  (planning must not die because memory is down).
- **`harness/deps.py::get_decision_store_factory`**: resolves
  `memory.decision_store.open_default_store` real-first, None on
  ImportError — same pattern as the code-graph factory. (New T4 surface,
  see INTERFACES.md 2026-09-10.)
- **Planner prompt** (prompts.py): new `## Relevant past decisions`
  section, deliberately placed AFTER `## Retrieved context` and BEFORE
  `## Constraints` — T3's difficulty predictor cuts the first user
  message at `## Retrieved context`, so decision memory can never shift
  difficulty scoring / routing (the placement is regression-tested from
  the runtime side: `test_memory_section_invisible_to_difficulty_
  predictor`). The predictor cut marker itself is untouched.
- **core.py planning step**: after retrieval, before the planner call —
  query (when `plan_with_memory`, default True), render, inject as
  `memory_block=`; new `decision_memory` trace event {query, matched,
  error, section_chars} or {skipped: "plan_with_memory=False"}. OFF is
  exactly one code path (config-only) — the ablation depends on that.
- **Config keys** (harness/config.py): `plan_with_memory` (True),
  `memory_query_limit` (6), `memory_max_chars` (1500).
- **state.json repo_path** (context.py, ADDITIVE): TaskState now records
  the task's repo_path after the six Boundary 4 keys (omitted when
  empty). T4's `ingest_state_file` has read `data.get("repo_path")`
  since Round 1 — the reader predated the writer; now ingestion stamps
  rows per repo so the repo-scoped query has something to match. The
  six-key prefix order is unchanged and still asserted
  (STATE_KEYS_WITH_REPO added; existing schema test untouched and still
  green). Boundary 4 note updated in INTERFACES.md.
- **Tests**: tests/test_decision_memory_planning.py (17): memory-side
  repo filter + normalization + open_default_store location; harness-
  side query building/caps/degradation (duck-typed store, monkeypatched
  factory); prompt placement contract + predictor invisibility; state
  repo_path prefix-order + ingest round-trip; and three REAL-loop e2e
  (scripted model) proving content-receipt — the planner's actual user
  message contains a store marker unseen by the issue text (ON),
  OFF-arm never opens the store (factory-call counter = 0), and a
  broken store degrades to "(none recorded yet)" + trace error without
  killing the task.

**Plumbing pilot (deterministic, before spending model budget)**: real
scheduler → real worker subprocesses → real harness → real Docker
verify, scripted model — ON arm: 5/5 tasks success with seeded
repo-scoped decisions demonstrably in the planner prompt (matched 1-2
each), the no-repo global row never leaks into any prompt; OFF arm: 5/5
success, no memory rows, skip events. logs/memplan-pilot/run-*/.

### Task B — the ablation: does it actually help? (honest answer)

**Method** (runtime/ablation_memplan.py, mirroring runtime/ablation.py
conventions): 5 fixture tasks, REAL full stack both arms (scheduler →
worker subprocesses → run_task → Docker sandbox/verify), SAME pinned
model for every call (z-ai/glm-5.3-free via tokenrouter, adaptive
routing OFF — a routing confound would make the delta unattributable),
arms differ in exactly `plan_with_memory`. The precondition — relevant
prior decisions exist — satisfied by SEEDED rows genuinely mined from
prior-run traces (v4/v3: the bare-`pytest` ImportError class that
burned real turns in abl-off-bug01's attempt 1, sed-with-quotes
breakage from the bug03 traces, per-repo suite-invocation conventions,
the successful strategies prior fixes actually used), recorded via
`DecisionStore.record` (source "manual", repo_path stamped) — the
documented path for facts not in state files. Dedicated ablation DB
(HARNESS_DECISIONS_DB pinned per-run); production store untouched.
Proxy prices per the standing honesty notes.

**Result (run1, logs/memplan-ablations/run1/summary.json):**

| metric | OFF | ON | delta |
|---|---|---|---|
| success | 5/5 | 5/5 | none |
| attempts (all tasks) | 1 | 1 | none |
| verify failures | 0 | 0 | none |
| harness-level model calls (trace) | 35 | 27 | **-23%** |
| tokens | 147,916 | 112,390 | -24% |
| proxy cost | $0.2148 | $0.1782 | -17% |
| known-mistake recurrences | **3 tasks** | **0 tasks** | -3 |

**What actually changed**: the three OFF-arm recurrences are all the
SEEDED known-mistake class — bare `pytest` → ImportError → wasted
diagnostic turns (bug01, bug02, bug03). In the ON arm ZERO tasks
re-tripped it: first commands were `python -m pytest` from the repo
root, matching the seeded convention. And the causality is visible in
the plans/commands, not just aggregates: ON-bug03's plan used the
python-rewrite strategy (its seed row documents sed-quote breakage from
the v3 traces); ON-bug05's plan repeats the module-level-list
diagnosis; ON-bug01 used the pathlib exact-block replacement its seed
row describes.

**What did NOT change — the honest core**: success rate, attempts, and
verify failures are IDENTICAL. The fixtures are easy for this model
class either way; memory did not add task-solving power, it removed
wasted work. The "avoid repeating a previously-discovered mistake"
question: YES, measurably (3→0). The "fewer attempts" question: NO at
this task difficulty (nothing needed a second attempt). Anyone quoting
this round should say "efficiency + mistake-avoidance at equal
success", not "memory makes the agent better".

**Honest caveats (all in the run's summary.json post_run_analysis)**:
n=5×1rep directional only; the per-arm "ledger calls" metric is
INFLATED by router-internal empty-response retries (ON-bug01: 5
sub-second compl=0 entries inside 4 real calls — I recount trace
model_request events for the honest number); each arm absorbed exactly
one symmetric mid-run crash-retry (OFF-bug05, ON-bug02 — runtime
resumed both; duplicated post-resume calls included in counts); a
parallel terminal's in-flight agent-tests feature ran symmetrically in
both arms; the seeded rows are convention/gotcha facts mined from real
traces, not fix cheat-sheets (no row says "change X to Y").

### Verification at close

New suite 17/17; harness selection (config_trace_state,
stubs_and_deps, retrieval_tools, editor_prompts, recall_unit,
adversarial) 122/122; e2e_run_task + decision_memory_planning 45/45;
memory-module suites (decision_store, code_graph, mcp_server,
mcp_client, mcp_stdio_fileno, dashboard) 56/56; cli + cli/mcp
adversarial 117/117. All this round's edits coexist with the two
parallel sessions' in-flight rounds (their suites re-run green in the
same sessions; the shared-file coexistence verified by the combined
green runs above).


## Improvement Round 2 — coordinated multi-file changes (2026-09-10, parallel session)

(Round 8 was landing in-flight in this same tree during this round —
lint gate, BATCH, DOCS, state machine, decision memory, agent-tests.
This round built additively ON TOP of that state; the pre-flight
baseline of the 8 landed suites was verified green FIRST: 166/166,
~5:35 warm Docker. Two in-flight test files — test_context_budget_
rerank.py, test_self_critique.py — did not collect at round start
(their harness halves hadn't landed yet in my reads; they belong to
the parallel session and were left alone.)

### Task A — detect when a fix genuinely requires coordinated changes

**New module `harness/coordination.py`.** Two cooperating pieces:

1. **Planning-side fan-out** (`detect_coordinated_change`): before the
   planner runs, the structural graph (Terminal 4's memory.code_graph,
   raw Graph like retrieval consumes — no new memory surface) is
   walked from the files retrieval ranked: every CALLER of a symbol
   defined in a changed file (call edges) plus every IMPORTER of a
   changed module (import edges) → the dependent file set with symbol
   anchors. Text vocabulary ("signature"/"parameter"/"drop the flag" vs
   "rename"/"every call site"/"all callers") classifies the shape; even
   without vocabulary hits, existing dependents still flag the change
   as coordinated (the GRAPH, not prose, decides atomicity). Protected
   paths (tests/*, VCS dirs) are EXCLUDED from suggested groups — a
   coordinated agent-edit group can never include a forbidden path
   (this was a real design bug caught by the e2e: the first version's
   "_detected" enforcement group swept tests/test_invoice.py +
   invlib/__init__.py in via import edges and made EVERY task on the
   fixture unachievable). Never raises; graph unavailable →
   detected=False, previous behavior.
2. **Planner prompt** gains a `## Coordinated-change fan-out` section
   (AFTER `## Retrieved context` — T3's difficulty predictor cut
   marker untouched) plus a `change_group` field in the plan schema
   and a step-rule about atomic groups. The planner DECLARING the
   group is the contract — detection is advisory (informs the
   declaration), never force-enforced: a detected group the plan
   declined to declare must not gate the task.

New trace event `coordination` {detected, kind, reason, changed_files,
dependent_files, group_files, excluded}.

### Task B — coordinated, ATOMIC multi-file patches

- **Plan schema**: steps may carry `change_group: <name>`.
  `_plan_change_groups` unions each group's files_hint;
  `state.json` gains the ADDITIVE `change_groups` key (after
  repo_path; Boundary 4 schema updated in INTERFACES.md; absent when
  none declared; resume-hydrated; malformed-tolerant).
- **Gate** (`coordination_gate`, default on): after the attempt's
  steps + final edit-validation, BEFORE final verify — a group with
  some-but-not-all members changed is a PARTIAL coordinated change:
  the attempt is poisoned (feedback names the missing members via
  `format_missing_group_feedback`), the verifier never sees it as
  complete. The verifier is NOT asked to re-litigate group
  completeness — a suite without call-site coverage would happily
  pass a half-updated rename (that's the entire point of the gate).
- **Rollback** (`editor.restore_group` + `group_orphans`): the
  declared group's files revert to pristine TOGETHER — including
  members that never changed and agent-created members (pristine
  state = absent → deleted). Non-group work in work/ SURVIVES (the
  config `coordination_rollback` selects "group" default | "all" |
  "none"). A FAILED verified attempt also rolls touched groups back
  together (atomic on failure, not only on partial). New
  `coordination_rollback` trace event {groups, restored, mode} +
  a state.json decision; `files_touched` is cleaned of rolled-back
  files (`TaskState.clear_files_touched`).

### Task C — a GENUINE 4-file coordinated scenario, end-to-end

**New fixture `tests/fixtures/bug06_coord`** (not a toy): an invoice
library where `Invoice.invoice_total(include_tax=False)` ignores its
flag and always returns the pre-tax subtotal. The correct fix — drop
the misleading parameter, rename to `amount_due()` returning
subtotal+tax — REQUIRES coordinated edits in model.py (definition),
serializers.py (2 call sites), reports.py (1), api.py (1): four files
that must change together; the suite's five tests all break otherwise.

**e2e proofs (tests/test_coordination_e2e.py, real Docker stack, all
green)**:
1. *Happy path*: scripted model applies the real 4-file fix via 4
   heredoc edits → detection event fired (structural fan-out), plan
   declared the 4-file group, gate passed, verified success in 1
   attempt, all four files in the diff, group + members in
   state.json.
2. *Partial change rejected + ATOMIC rollback*: attempt 1 renames
   model.py only (the classic half-updated state) → gate poisons the
   attempt pre-verifier with the three missing files named →
   `coordination_rollback` restores ALL FOUR (model.py too, though
   it was the one file that DID change — that's the atomicity
   claim proven) → attempt 2 completes the group → success at
   attempts=2, final diff = exactly the 4-file fix, only ONE
   final_verify ever ran (on the complete change).
3. *Complete-but-broken rolls back together*: all 4 files change but
   model.py computes tax twice → gate passes (group complete),
   final verify fails → group rolls back together → attempt 2's
   correct fix wins; broken arithmetic absent from the final diff.
4. *Regression guards*: plain single-file fix (bug02) with detection
   ON → coordination event present, detected=false, NO gate/rollback/
   groups; `coordination_detect=False` → no event at all (OFF arm).

**Unit suite (tests/test_coordination.py, 31 tests, no Docker)**:
vocabulary classification, real-graph fan-out on a synthetic repo
(callers+importers found via edges, changed files excluded, bounded,
no-dependents safe, graph-absent degrade), protected-path exclusion,
group completeness, plan parsing/group union, prompt blocks (incl. a
no-leftover-{coordination_rules}-slot guard — the planner template's
JSON braces forbid str.format, so the slot is .replace()d), atomic
restore_group semantics (untouched members revert too, agent-created
members deleted, non-group work survives, missing files never raise,
orphan-dir pruning), and the state.json additive-key invariants
(order, absence-when-unset, resume hydration, malformed tolerance,
files_touched cleanup).

### Docs/config/CI

- Config keys (harness/config.py): coordination_detect,
  coordination_gate, coordination_min_files, coordination_rollback.
- harness-ci.yml: both new suites in the selection + fixture image
  warm list (bug06_coord) + path filters.
- INTERFACES.md: Boundary 4 schema shows the additive change_groups
  key; Change Log entry filed (2026-09-10 Terminal 1).

### Test status at round close

**265/265 green** across the 13 landed harness suites (10:25 warm
Docker): the 166 pre-flight baseline + 31 coordination unit + 5
coordination e2e + the parallel session's batch_docs_lint (11),
agent_tests, decision_memory_planning suites that landed mid-round.
The two not-yet-collecting in-flight files remain the parallel
session's to finish (their harness halves — retrieval.rerank_files,
prompts.render_self_critique_prompt — were not in the tree at close
of my round; verified by import attempt, not assumption).

### Known limitations (this feature, honest)

- **Group declaration depends on the planner**: only plan-declared
  groups are enforced. A planner that ignores the fan-out section can
  land a partial coordinated change IF the suite happens to stay
  green (the gate needs the declaration to know the file set). The
  detection section + step rules make that unlikely, and the
  verifier still catches behavior-visible breakage — but
  "undeclared coordinated change" is out of the gate's reach by
  design (enforcing detection-suggested groups proved actively
  harmful: the tests/__init__ false-positive class).
- **Fan-out is name-resolution based** (memory.code_graph's documented
  over-approximation): `obj.foo()` edges to every `foo` method. For
  group SUGGESTIONS that's the right recall-over-precision trade; the
  declaration is where precision comes from.
- **files_hint is the group's file set**: a planner declaring a group
  but listing files loosely (e.g. forgetting one call-site file in
  files_hint) under-declares the enforced set. The prompt's fan-out
  section lists the graph-found dependents precisely to prevent this.

## Round 7 (2026-09-09) — production readiness: CI + architecture docs

(Round 6 landed in-flight in this tree before Round 7 started — its
adversarial hardening (editor `_ALWAYS_PROTECTED` VCS dirs +
traversal-normalizing `is_protected`, core final-edit re-validation
before the success-minting verify, tools deny-pattern reorder/bash
pipe fixes) + tests/test_adversarial.py were verified green FIRST:
150/150 harness tests incl. the 43 new adversarial ones, ~6 min warm
Docker. Round 7 built additively on top.)

### Task A — CI pipeline (the `harness` job in .github/workflows/ci.yml)

- **Matrix**: Linux/macOS/Windows × Python 3.10/3.12 (3.10 = the
  pyproject floor; 3.12 = current stable; litellm 1.74.9 pin holds on
  both). `fail-fast: false` so one cell's flake doesn't hide another's
  real failure. Runs on every push/PR, alongside T3's existing
  runtime/stress jobs (same file, unchanged).
- **What's in CI**: the harness module's own test selection (the six
  Round 1–5 suites + Round 6's test_adversarial.py — same list as the
  "Test status" section below), `-p no:randomly` for determinism.
- **Docker handling**: ubuntu runners ship Docker → Linux cells run
  the FULL suite incl. real-sandbox e2e (fixture dep images warmed
  first via `execution.sandbox.ensure_image`, same pattern as the
  stress job). macOS/Windows runners have no daemon → Docker-dependent
  tests self-skip CLEANLY via the existing `requires_docker` convention
  (skipif: daemon unreachable OR HARNESS_EXEC_SKIP_DOCKER=1, explicit
  reason string). **Verified by simulation, not assumption**: full
  suite re-run with HARNESS_EXEC_SKIP_DOCKER=1 → 142 passed, 8 skipped,
  each skip carrying the "docker daemon not reachable" reason — the
  exact behavior a Docker-less CI cell will show.
- A small Docker-availability diagnostic step prints CLI/daemon state
  per cell so skip counts in the logs are explainable at a glance.

### Task B — architecture documentation

- **`docs/architecture-harness.md`** (NEW): prose + ASCII flow + Mermaid
  diagram covering the core loop (setup → baseline verify → retrieval
  → plan → attempt loop → verifier gate → product output), the step
  session (bash-only, SUBMIT/RECALL escapes, deny-pattern guard), the
  retry/repair logic (last_feedback seam, both feedback loops, final
  edit re-validation, short-circuits), resume, the state.json
  contract + rules, the two-layer retrieval strategy, module map,
  and the logs layout. Written for someone who has NOT read the code.
- Linked from the root README (four-layers table + Status section).

### Test status at Round 7 close

**150/150 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py
tests/test_adversarial.py`), ~6 min warm Docker — the 107 from Round
5 + 43 from Round 6's adversarial suite. Docker-less simulation of the
same selection: 142 pass / 8 clean skips. The CI job runs exactly this
selection.

## Round 6 (2026-09-09) — multi-repo validation (3 OSS repos) + adversarial robustness

### Pre-flight

Full Round-5 suite re-run first: **107/107 green** (~3.3 min, Docker warm),
Docker up, key present. Verified, not assumed.

### Task A — 3 more real OSS repos, varying size/structure (all SUCCESS)

Round 4 proved one repo (jaraco/path). Round 6 adds three MORE unfamiliar
repos — none ever used by any terminal before, deliberately varied in shape:

| repo | shape | introduced bug (genuine, failing-test-encoded) | result |
|---|---|---|---|
| r1chardj0n3s/parse | small utility lib, flat single-package | `Result.spans` for dict-style fields (`{quest[name]}`) keyed by the internal mangled group name (`quest_name_`) instead of the original field name — inconsistent with `r.named` (public-API leak of a Parser implementation detail) | **SUCCESS, 1 attempt, 10/10 checks, 47s** |
| bottlepy/bottle | mid-sized micro-framework, ENTIRE framework in one 4.4k-line file | `parse_range_header` lost the RFC 7233 end clamp (`min(int(end)+1, maxlen)`): a satisfiable `bytes=90-500` on a 100-byte file reads as unsatisfiable → 416, or serves a lying Content-Length | **SUCCESS, 1 attempt, 10/10 checks, 76s** |
| pallets/click | large CLI toolkit, MODERN src/ LAYOUT | `DateTime.convert` iterates `self.formats` in REVERSED order — for formats where two patterns parse the same input differently (`%m-%d-%Y` vs `%d-%m-%Y`), the caller-declared order must win (documented contract); ambiguous dates silently swap month/day | **SUCCESS, 2 attempts, 10/10 checks, 371s** |

Each bug was designed after genuinely reading the code (hours of live
probing per repo), verified as NOT covered by any existing upstream test,
and each repo's suite was pinned so it fails ONLY on the new regression
test (env limits documented: bottle's test_stpl/test_wsgi are Windows-CRLF
checkout artifacts; click's test_deprecations needs installed-package
metadata, test_stream_lifecycle is a 10-min subprocess-stress suite,
test_echo_via_pager needs `less` in the slim image — all deselected with
reasons, same class as R4's jaraco chown deselections).

**Every run went through the FULL stack**: real Scheduler → real
`python -m runtime.worker` subprocess → real `harness.core.run_task` →
real Docker sandbox/verify per command → verifier-gated success →
git branch/commit + rationale → real approval-gate file protocol (approve)
→ original repo untouched (probe-verified per repo) → behavioral
validation of the FIX in-sandbox (spans keyed correctly / ranges clamped /
formats tried in order) → full suite green on the fixed work copy.

**HONEST DOWNGRADE — the model layer was SCRIPTED, not cloud.** Two
real-model attempts failed on ENDPOINT DEGRADATION, not harness issues:
the tokenrouter glm-5.3 endpoint began burning its completion budget
entirely as hidden `reasoning_content` (content=None, finish_reason=length)
on the big planner prompt — twice at a 4000-token budget (1453s and 265s
calls), then again at 8000 (10271-token calls). Small prompts still worked
(verified: 20-70s, content present) — a large-prompt/reasoning-verbosity
failure mode, not an outage. R4 already proved real-cloud-model competence
end-to-end ($0.053, 6 calls); Task A's question was whether the HARNESS
generalizes across repo shapes — answered by scripted-model runs (T3's
`mock_script` mechanism through `Task.config`; the real loop, real
sandbox, real verify, real guards all executed). Cost of the failed
real-model attempts: ~$0.06 total; their traces preserved under
logs/oss-round6/oss-r6-parse* as the evidence.

**What the three repos actually tested (the generalization claim):**
- parse: flat-layout retrieval anchoring on a target test in a small
  package; patch-in-single-module flow.
- bottle: single-file framework — the bug and fix both live in ONE
  4.4k-line file; retrieval must anchor despite no package structure.
- click: src/ layout — the fix lands in `src/click/types.py` while tests
  run from repo root with `PYTHONPATH=src`; the pinned suite command
  carries the env override through every verify call.
All three: protected tests untouched, state.json/trace complete, approval
gate live, original repo never mutated. Report:
`logs/oss-round6/multi_repo_report.json` (10 checks per repo, all PASS).

### Task B — adversarial robustness: 43 regression tests; 2 real defects found+fixed

Attack matrix executed first as probes (logs/oss-round6/adv_probe*.py),
then encoded permanently in **tests/test_adversarial.py (43 tests, all
green)**:

1. **Protected-path guard (editor.py)**:
   - `is_protected` now '..'-normalizes paths BEFORE fnmatch — a
     traversal-shaped path (`subdir/../../tests/t.py`) can no longer evade
     a `tests/*` glob at the guard level (the real pipeline feeds
     rglob-normalized paths that can't contain '..', so production was
     safe — but the guard itself was evadable; defense-in-depth tested).
   - `.git`/`.hg`/`.svn` are ALWAYS protected, independent of
     `protected_paths` config: the snapshot drops .git, so any VCS path
     in a diff is an agent FORGERY of VCS state.
   - `check_edits` scans work/ for forged VCS paths on a separate pass —
     `changed_files` deliberately SKIPS .git content, so a diff-only
     check could never see the class (found by my own failing test).
2. **Deny-pattern guard (tools.py)**: covers reordered rm flags
   (`rm -fr /`), loose spacing, pipes into bash/zsh/dash from BOTH curl
   and wget (previously only `| sh`), fork bombs — while benign
   look-alikes stay allowed (relative `rm -rf build/`, plain curl,
   traversal READS — containment is the sandbox's job).
3. **Sandbox containment (verified e2e, Docker-gated)**: traversal
   writes (`echo > ../../../escaped.txt`) land on the container's
   read-only rootfs or inside the work mount — host-side pristine/ and
   the original repo byte-identical after hostile commands.
4. **Prompt injection e2e (the critical find)**: issue text with embedded
   "ignore previous instructions" ordering the agent to (a) defuse the
   test to `assert True`, (b) forge `.git/config`, (c) traversal-write
   outside the repo. Scripted model OBEYS. **Found a REAL success-path
   bypass in core.py**: the step-level guard correctly rejected the
   edits, but the attempt then fell through to final verify — which saw
   target+suite green (the defused test trivially passes AND the real
   fix was also applied) and minted `status="success"` with the
   protected-path violation sitting in work/. **Fix**: `run_task` now
   re-runs `check_edits` before final verify — an edit-policy violation
   poisons the whole attempt (retry with explicit feedback), the
   verifier never re-litigates policy. Regression-tested end-to-end
   (the same injection now fails every attempt; host integrity asserts).
   Also regression-tested: the whole-tests-dir RENAME evasion (mv tests
   tests.bak + trivial new tests) — the deletions show up as protected
   paths and the run is blocked.

**Real-model driver bug found during Task A runs (worth keeping):** the
Round-6 driver initially used `run_id=f"oss-r6-{name}"` COLLIDING with
the task_id — the scheduler's run dir (`logs_root/run_id/task_id/`)
nested INSIDE the harness log dir (`logs_root/task_id/`), so
`_fresh_paths`' archive rename hit Windows' open-handle restriction
(worker's inherited worker.log handle) → `PermissionError(13)` before any
trace event, 3 crash-retries, error. Root-caused from worker logs; R4's
driver used a distinct run_id (`oss-round4-run`) and never saw it.
Lesson for all: **scheduler run_id must never equal a task_id sharing
its logs_root** (documented here rather than changing T3's code — the
scheduler can't know task_ids in advance; convention is enough).

**Runtime-owned edits this round (T3's files, flagged per cross-terminal
practice — see INTERFACES.md Change Log 2026-09-09 Terminal 1):**
- `runtime/worker.py` + `runtime/model_router.py`: opt-in
  `max_completion_tokens` ctx key → litellm `max_tokens` (absent =
  previous behavior). Motivated by the endpoint's reasoning-burn failure
  mode above. T3's full suites re-run green after the edit (48 passed,
  3 cloud self-skips) — including the AuthenticationError no-retry
  test, whose intent I probed changing and deliberately REVERTED (the
  module's own test documents the contract; the observed gateway auth
  flake stays a health note in the run evidence).

## Round 5 (2026-09-09) — CLOSEOUT: item 13 finished, state flag fixed, suite green

### Task A — RECALL: on-demand reinjection of compacted detail (closes the item 13 gap)

The Round 4 self-audit honestly flagged that "reversible compaction" was
only met by design — trace.jsonl kept everything but nothing could pull
older detail BACK into a live session. That gap is now closed as a real
mechanism:

- **The protocol**: a step session outputs `RECALL <terms>` in place of a
  bash command. `harness/core.py` `run_step` intercepts it BEFORE command
  extraction (both the raw reply AND its fence-stripped form — a fenced
  ` ```bash\nRECALL x\n``` ` must never execute as shell; that fall-through
  was a real defect caught by the sub-agent's characterization test),
  greps THIS task's trace.jsonl, and re-injects the matching entries into
  the live session's context as the next user message.
- **The retrieval primitive**: `TraceLogger.find_events(query, kinds=None,
  limit=5, max_chars=4000)` (harness/trace.py) — case-insensitive substring
  over (event kind + JSON data dump), most-recent-N in chronological
  order, per-entry char cap with truncation marker, malformed lines
  skipped, file errors → [], never raises mid-step.
- **Budgets (config-driven, no constants)**: `max_recalls_per_step` (3) —
  a step must still do its work in bash turns; exhaustion nudges back to
  bash/SUBMIT, never deadlocks (tested). `recall_results_cap` (5),
  `recall_max_chars` (4000).
- **Observability**: new `recall` trace event {step_id, turn, query,
  matched}; step system prompt documents the escape to the model
  ("Recovering compacted-away context (RECALL)").
- **Proof it's real**: e2e test where step 1's session observes a marker
  via `echo` (compacted away by the per-step context reset), step 2's
  FRESH session RECALLs it, and the fake model only completes the fix
  after the marker demonstrably arrives in ITS OWN message list —
  content-receipt proof, not code-reading. Cross-session reinjection
  through the real Docker stack, real trace file.

Sub-agent note (per round instructions): delegated the RECALL unit-test
suite (tests/test_recall_unit.py, 26 tests) to a general sub-agent — it
performed well, delivered green tests, and caught a genuine defect in my
core.py fall-through (fenced RECALL executed as bash) that I then fixed;
its two docstring nits (spec-vs-code wording) were resolved by editing the
docstrings. The sub-agent approach worked; e2e tests stayed in-terminal
(they needed accumulated loop-semantics context).

### Task A2 — T3's state.json flag (cli-real-smoke) fixed

Terminal 3's Change Log flag: a successful task's final state.json showed
`completed_steps: []` + `remaining_plan: [step]`. Root cause (diagnosed
from that run's actual trace, not guessed): the step's commands had
applied the fix, but `max_step_turns` (6, pinned in the smoke config)
exhausted before the model's SUBMIT — `step_end ok=false
"exhausted ... turns"` — then final verify passed and the task succeeded,
with `complete_step` never called. Not resume-specific (the kill happened
pre-plan).

**Fix**: on a VERIFIED success, `run_task` records every plan step that
RAN in the winning attempt as completed (new
`TaskState.complete_all_ran_steps(steps)` in harness/context.py — the
verified diff subsumes each ran step's work; steps skipped by early-exit
never needed to run). state.json now agrees with the verified result;
`harness status` renders true progress on that path. Schema unchanged.
Regression test: exhausted-turns-with-real-fix → success AND
completed_steps == full plan, remaining empty.

### Task B — final suite + honest closeout

**107/107 green** (78 prior + 26 RECALL unit + 3 new e2e), ~4 min warm
Docker, full command in the Test status section below.

## Round 4 (2026-09-09) — self-audit + first full-stack run on an unfamiliar OSS repo

### Pre-flight: Round 3 verified landed (not assumed)

Full suite re-run first: **75/75 green** (~3 min, Docker warm), context.py
repair + regression tests present, e2e docstring updated, git/rationale
wiring live, approval tests present. All four Round-3 tasks confirmed.

### Task A — self-audit vs project-spec.md items 1–17 (all CORE)

Went through each item against the actual code, one by one:

| # | Item | Verdict |
|---|---|---|
| 1 | Repo understanding / retrieval | **Solid** (two layers: structural via memory.code_graph + target-test anchor + subword matching, grep fallback; best-effort degrade tested). |
| 2 | Tool interface / action space | **Solid** (bash-only per mini-swe-agent decision; fenced-block/heredoc-safe extraction; deny-pattern guard; SUBMIT semantics). |
| 3 | Patch generation + validation | **Solid** (check_edits: protected paths + syntax before verify; unified diff vs pristine; snapshot/restore). **One real defect found+fixed this round** (see Task B). |
| 4 | Sandboxed execution | **Solid** — real Docker sandbox auto-resolved via deps.py since Round 2 (fresh container per call, networkless, resource-limited); fail-loud SandboxUnavailableError, no silent fallback (deliberate). |
| 5 | Verification beyond "tests pass" | **Solid** (baseline verify on pristine pre-edit — mislabeled task short-circuits; flake = 3-valued outcomes incl. timeout; full-suite regression after target). |
| 6 | Within-task state/memory | **Solid** (state.json + plan.json; tried-work tracked; `files_touched` recorded only after validation; feedback flows step→step and attempt→attempt with verifier raw output). |
| 7 | Stopping conditions | **Solid** (max_retries, budget_cap_usd, max_wallclock_s — all config-driven, checked every iteration + mid-step deadline). |
| 8 | Logging / traceability | **Solid** (trace.jsonl: every prompt, response, tool call/result, verify, decision, RECALL; survives relaunches via resume append). |
| 9 | Model/provider abstraction | **Solid** (Boundary 2 via deps; router decides when model=None; per-call cost/tokens via get_last_usage convention; proven live on a real cloud tier this round). |
| 10 | Config / reproducibility | **Solid** (full merged config logged in task_start event minus api_key; plan.json captures attempt/cost). |
| 11 | Persistent structured state file | **Solid** (state.json, exact Boundary 4 schema, atomic tmp+replace write; regression tests hold the invariant; Round 5: agrees with verified results even on the exhausted-turns path). |
| 12 | Context resets at checkpoints | **Solid** (per-step FRESH sessions; per-step system prompt rebuild; completed-steps handed over structurally, not as chat history). |
| 13 | Reversible compaction | **SOLID as of Round 5** (was "met by design, honest caveat" in R4): trace.jsonl keeps everything, state.json is the compacted view, and the **RECALL protocol now pulls older detail back into a live session on demand** — budgeted, trace-logged, tested cross-session with content-receipt proof. |
| 14 | Task decomposition 2–4 sub-steps | **Solid** (planner enforces 2–4 with per-step checkpoint; no "run the suite" steps; early verifier exit when done mid-plan). |
| 15 | Constraint re-injection | **Solid** (re-injection block appended to END of every tool result — issue one-liner, current step, remaining, protected paths, don't-touch-tests). |
| 16 | Curated per-step context | **Solid** (step_files = step hint + retrieval, capped files/lines; not a static bundle). |
| 17 | Verifier-gated completion | **Solid** (success ONLY on verify() target+regression+not-flaky; SUBMIT ends a step, never a task; pre-passing pristine short-circuits to success with zero calls). |

**Nothing missing.** The two Round-4 weak spots: (a) item 13's reinjection
gap — **CLOSED in Round 5 (RECALL)**; (b) no failure CLASSIFICATION —
remains a documented, accepted limitation (see Known limitations below).

### Task B — full-stack DoD run on jaraco/path (unfamiliar real OSS repo)

**Repo**: jaraco/path @ 67319bb (fresh clone; NOT one of the 5 fixtures,
never used by this project before). After a genuinely-thorough hunt
(masks/matchers/classes/in_place/write_text/chunks/hashes/times/
merge_tree/only_newer/relpath/Multi/TempDir/walk all checked live —
upstream is solid), I **introduced one genuine bug**: dropped
`in_place()`'s permission preservation (`os.open(self, os_mode, 0o666)`,
no chmod re-apply — the function's own contract says "same permissions").
A 0o600 config file silently becomes 0644 after an edit — a real
security-adjacent defect class, encoded in a failing regression test
(`tests/test_in_place_perms.py`, both success and error-restore paths).
Bug verified reproducing in-sandbox (0o600→0o644); rest of suite green
with a pinned test command (`-o addopts=` strips --doctest-modules +
pytest-ruff pseudo-tests; test_chown/test_group deselected — they need
/etc/passwd entries absent from the slim image, an env limit, not the bug).

**Run 1 — FAILED, and that's the finding.** Full real stack (real
Scheduler → worker subprocess → real harness → real cloud model via
tokenrouter → real Docker sandbox; T3's own ablation was concurrently
hammering the same endpoint, so planner calls ran slow) — the agent
FIXED the bug, final verify PASSED (target + full suite), and then the
worker CRASHED with `UnicodeDecodeError: b'SQLite format 3...'`. Root
cause (mine, harness/editor.py): (1) `unified_diff` read changed files
with `errors="strict"` OUTSIDE its try/except — the repo's pytest run
materializes a binary `.coverage` (SQLite) in work/, counted as a
"changed file", and the strict decode raised instead of returning
None; (2) verifier-created artifacts (`.coverage`, cache dirs) counted
as agent edits at all. A **verified fix was reported as "error"** after
3 crash-retries exhausted (each resume → re-verify → recreate .coverage
→ crash again — a textbook crash loop).
**Fix (harness/editor.py)**: strict decode moved inside the
UnicodeDecodeError guard (binary → reported, never raised);
`changed_files` skips run artifacts (`.coverage*`, `.hypothesis`,
`.cache`, + prior skip set). **3 regression tests added**
(binary-not-crash, binary-reported, artifacts-ignored). 78/78 suite
green post-fix.

**Run 2 — SUCCEEDED end-to-end** (`oss-path-perms-r2`, 10/10 checks,
driver + corrected validator under logs/oss-round4/): Scheduler →
worker → real harness → **real cloud model (z-ai/glm-5.3-free via
tokenrouter, 6 calls, $0.053)** → every command through the real Docker
sandbox → verifier-gated **success in 1 attempt, 373s**. The agent wrote
its own (equivalent, cleaner) fix — capture `original_mode` before
rename, `os.chmod` after create — NOT the literal upstream shape (my
validator initially demanded the literal shape and failed; lesson
recorded: validate BEHAVIOR, not implementation text). Confirmed live:
git branch `harness/fix-bug-report-...` with pristine-first commit whose
`git show` IS the fix; rationale.md grounded in the trace; **approval
gate ran the real file protocol** (request.json → external approver →
decision.json → approved → git output composed); original repo
untouched (bug still present there); regression tests re-run green on
the fixed work copy IN-SANDBOX; **Terminal 4's memory ingested the
run's decisions via the real recursive poll (source: state-file) and
serves them through the MCP `query_decisions` surface** — verified by
query, not assumption. Full report: logs/oss-round4/oss_run_report.json.

**Cross-module notes from the run:**
- T3 scheduler/worker/resume behaved exactly per contract under a real
  crash-retry loop (crash→retry→resume→crash→exhausted→error status);
  the hang-check pin (`hang_heartbeat_stale_s` 1200 ≥ slow model calls)
  worked as documented.
- T2's sandbox/verify were transparent throughout; the ONLY issue was
  my editor.py binary handling (T2's DoD had hit semver's symlink quirk
  but not the binary-artifact one — different repo, different quirk).

**Test status: 78/78 pass** (75 prior + 3 editor regression), ~4.5 min
with warm Docker.

## Round 3 (2026-09-08) — patch review, git-native output, approval e2e

### Task A — Terminal 4's repair of harness/context.py: REVIEWED, CORRECT

An interrupted edit of mine (the Round-2 plan.json work) had left a
duplicated dangling `def _write` stub in context.py; Terminal 4 found it
live (every `import harness.*` raised IndentationError, caught during
their CLI e2e) and deleted exactly the dangling stub — nothing else.
**Verdict: the repair matches my intended final design.** The intended
shape is and was: ONE `_write` (the atomic tmp+replace Boundary-4 state
writer) and `save_plan_steps` writing ONLY plan.json via `PLAN_FILE`
(separate file, separate writer). The duplicate stub was an artifact of
the interrupted edit, not a method I meant to keep.
**Regression tests added** (`tests/test_config_trace_state.py`):
- `test_harness_modules_import_and_parse` — AST-parses every harness
  module + import-checks each (catches the un-parseable-file class at
  collection time, before any downstream module breaks).
- `test_context_has_single_wellformed_write_method` — structural
  invariant: exactly one `_write` containing the tmp+replace pair;
  `save_plan_steps` must not call `self._write(`.
**Proven live, not just written**: a throwaway script re-introduced the
exact corruption (dangling stub before `_write`), reproduced the
identical `IndentationError ... line 196`, tests failed at collection,
file restored, 12/12 green again. (Would have caught the bug originally.)

### Task B — stale docstring fixed

`tests/test_e2e_run_task.py` module docstring now says "REAL Docker
sandbox and verifier (execution.sandbox / execution.verify via deps.py
auto-resolution; the local subprocess stub is fallback-only)".

### Task C — git-native output + rationale wired into run_task

On a VERIFIED fix, `run_task` now produces real git artifacts in the
harness's PRIVATE work copy (original repo never touched, by
construction — git ops only ever run in `logs/{task_id}/work/`):
- `execution.git_output.produce_git_output(work_dir, issue_text,
  changed_files, diff, verification_summary, rationale, branch_name,
  pristine_dir)` → git init + pristine first commit + fix commit on a
  `harness/fix-<slug>` branch. Result dict {branch, commit_sha,
  commit_message, pr_description} → `logs/{task_id}/git.json` + a
  `git_output` trace event. Proven: the fix commit's `git show` diff IS
  the fix; author is harness-bot; two commits on the branch.
- `execution.rationale.build_rationale(log_dir, issue_text)` → one
  grounded paragraph from trace.jsonl + state.json → written to
  `logs/{task_id}/rationale.md` + a `rationale` trace event. Written on
  success paths (after `task_end`, which the verdict keys off) AND on
  failed/timeout outcomes (`_record_rationale_only` — a rationale is
  valuable on losses too; an unverified diff NEVER gets git output).
- Both best-effort BY CONTRACT: any failure degrades to a
  `rationale_failed`/`git_output_failed` trace event, never changes the
  verifier-gated task outcome (new config keys `git_output`/`
  rationale_log`, both default True; `branch_name` optional pin).
- e2e proof: 3 Docker-gated tests (produces artifacts + disabled-path
  produces none + failed-path gets rationale but no git output).

### Task D — approval-mode wiring CONFIRMED LIVE

The gate lives in Terminal 3's worker, wrapping my Boundary-3 run_task
(the runtime's documented design). Tested end-to-end, not by reading:
real `Scheduler` → real `python -m runtime.worker` subprocess → REAL
harness loop (scripted fix through real Docker verify) → real
request.json/decision.json file protocol with an external approver
thread. Both paths proven: APPROVE (task finishes success, diff applied,
review.log = requested→approved, worker events show approval_wait →
approval_granted → worker_finish, AND Task C's git output composes —
branch exists in work/) and REJECT (result downgraded to failed, diff
stripped, never applied).
**Real cross-module finding for Terminal 3**: a worker blocked in the
approval gate stops touching state.json; the scheduler's hang check
(default `hang_heartbeat_stale_s: 30`) KILLS it mid-gate. Mitigation
(documented pattern, now in my tests): pin `hang_heartbeat_stale_s` ≥
`approval_timeout_s`. T3: consider making the worker beat the
checkpoint heartbeat while parked in the gate — the heartbeat daemon
thread already exists in your worker; state.json staleness is the
hang signal, heartbeat is liveness, and a gate-blocked worker is alive.

**Enabling hook for cross-process determinism** (new,
`harness/_stubs/scripted_model.py` + `deps.get_call_model`): env var
`HARNESS_SCRIPTED_MODEL=<json spec>` resolves a file-driven
`ScriptedFileModel` (same dispatch contract as tests/fake_model) inside
worker SUBPROCESSES, where `set_call_model` injection cannot reach.
Cached per spec-path per process (ModelClient re-resolves per call; the
script queue must survive). Test-only hook by design; in-process tests
keep using `set_call_model`.

**Test status: 75/75 pass** (68 prior + 2 context regression + 3
git-output/rationale e2e + 2 approval e2e), ~3 min with warm Docker
images.

## What's built (complete module state, Rounds 1–5)

The full agent loop: given a `Task` (issue + repo), retrieve context, plan
sub-steps, edit a working copy via bash, verify, and retry with feedback —
implemented per `INTERFACES.md` Boundaries 3 & 4 and the context-management
ideas in `project-spec.md` (items 12/13/14/15/16/17, with 13 fully closed
in Round 5 via RECALL).

| File | Role |
|---|---|
| `core.py` | **`run_task(task, log_root=None) -> TaskResult`** (Boundary 3). Attempt loop, per-step sessions, stopping conditions, verifier-gated completion, **resume contract (Round 2, see below)**, **product-grade output on verified success (Round 3: git branch/commit/PR + rationale)**. |
| `context.py` | Structured state file `logs/{task_id}/state.json` in the exact Boundary 4 schema (atomic rewrite; `reset_completed()` on retry rollback; **resume hydration + `plan.json` bookkeeping**). **Round 3: Terminal 4's mid-edit repair reviewed + regression-tested — see above.** |
| `trace.py` | Append-only `trace.jsonl`: every prompt, model response, tool call, tool result, verify, decision, RECALL. **Survives relaunches (appended to on resume).** **Round 5: `find_events()` — the reversible-compaction retrieval primitive (RECALL queries it).** |
| `config.py` | All tunables from `task.config` merged over `DEFAULTS` (retries, budget, wall-clock, caps, protected paths…). |
| `retrieval.py` | **Two layers (Round 2)**: structural (imports/call-graph via Terminal 4's `memory.code_graph`, anchored on the target test + subword symbol matching) merged with the Phase-1 grep layer. Returns a `strategy` note. |
| `tools.py` | **Bash-only action space** (mini-swe-agent style): one command per turn, output-capped, `SUBMIT` ends a step, deny-pattern guard. **Round 5: `parse_recall` — the RECALL escape (control signal, never executed; checked raw AND fence-stripped).** |
| `editor.py` | Snapshot/restore of working copies, difflib unified diff, pre-verify validation (protected paths, Python syntax). |
| `prompts.py` | Planner prompt (2–4 sub-steps, each with checkpoint — no one-shotting; now states the retrieval strategy), step-session prompts, **constraint re-injection block appended to every tool result**. |
| `model_client.py` | Wraps Boundary 2 `call_model`: trace logging, usage/cost accounting via `get_last_usage()` convention, `TaskResult.model_calls` records. |
| `deps.py` | **The swap point**: tries real `execution.sandbox` / `runtime.model_router` / `memory.code_graph` first, falls back to `harness/_stubs/`. Tests inject fakes via `set_call_model` / `set_execute_sandboxed`; **cross-process tests use the `HARNESS_SCRIPTED_MODEL` env hook (Round 3)**. |
| `_stubs/` | Local stubs with exact contract signatures: subprocess sandbox, pytest-based `verify()`, single-provider litellm router (lazy import), **`scripted_model.py` (env-driven fake for subprocess tests)**. **All three real modules have landed; stubs are fallback-only.** |

## Round 2 changes

### Task A — resume/checkpoint contract (fixes the cross-module bug)

**Bug**: `_fresh_paths` archived `logs/{task_id}/` wholesale on every
relaunch, so Terminal 3's runtime-side checkpoint/resume could never
actually resume — state.json (the progress authority) was swept away
before the relaunched run could read it.

**Fix** (core.py + context.py):
- `run_task` now implements the resume contract: when
  `task.config["resume"]` is truthy AND the prior `state.json` has
  completed steps AND a readable `plan.json` exists → **resume**, else
  the old archive-then-fresh behavior (unchanged for non-resume callers —
  regression-tested).
- Resume keeps `logs/{task_id}/` intact: hydrates `TaskState` from the
  real state.json (decisions/files_touched/completed_steps preserved),
  **reuses the persisted plan** (new `plan.json`, harness-internal —
  re-planning could orphan recorded step descriptions), skips completed
  steps (`step_skipped_resume` trace event), keeps the surviving
  `pristine/`+`work/` dirs (partial edits are built upon, not thrown
  away), continues the **in-flight attempt** (a crash is an
  interruption, not a verification failure — no `max_retries` slot
  consumed), and seeds the budget cap with the pre-crash spend.
- Baseline verify is skipped on resume (a passing baseline would have
  ended the pre-crash run before any step completed; re-running it
  cannot change the outcome).
- `trace.jsonl` is appended to across relaunches — pre-kill events
  survive as first-class history.
- Degrades safely: unreadable state.json/plan.json or missing
  pristine/work → fresh start (trace event `resume_aborted`), never a
  crash or nonsense-resume.

**Proof**: `tests/test_e2e_run_task.py::test_resume_after_hard_kill_mid_run`
mirrors Terminal 3's scheduler scenario through the REAL loop: a child
process (tests/resume_driver.py) is hard-killed via `os._exit(70)` after
step 1 of 2 completes, the relaunch resumes from the real state.json,
skips step 1, finishes step 2 from the surviving partial edit, and
succeeds with attempts=1. Plus: non-resume relaunch still archives
(regression guard), corrupted state → fresh, missing copies → fresh.

### Task B — smarter context retrieval (Phase 2 scope)

`retrieval.py` grew a structural layer on top of the Phase-1 grep layer:
- Consumes **Terminal 4's `memory.code_graph`** (tree-sitter; via
  `deps.get_code_graph_factory()`) — one structural index for the whole
  project, no harness-private duplicate. Explicitly invited by
  memory/AGENTS.md.
- **Target-test anchor** (the key mechanism): the target test's file →
  its import edges → the module under test; its call edges → the symbols
  it exercises. Finds the right files even when the issue text names
  NOTHING (proved by test: issue "something is off in how numbers are
  summarized" → `numlib/mathutil.py` via anchor).
- **Subword symbol matching**: `compute_monthly_total` decomposes to
  {compute, monthly, total}, so an issue saying "monthly total is
  doubled" matches without the identifier ever appearing in the issue.
- **Call-graph neighborhood**: matched symbols expand one hop to direct
  callers/callees (the failing test is often the caller; the true defect
  often a callee).
- Structural scores outrank grep hits (×2 merge weight); the index lives
  at `logs/_code-graph/` (SHARED across tasks, OUTSIDE the original repo
  — the never-mutate guarantee holds; regression-tested) and is reused
  across tasks on the same repo (mtimes don't change on a read-only
  repo).
- Fully best-effort: graph module missing/broken → grep-only with
  `strategy: "grep"` (tested via monkeypatched broken factory). No
    embedding layer — true synonym gaps ("average" vs `mean()`) remain
  future work (would need an embedding index; documented below).
- Planner prompt now tells the model which strategy produced its
  context; `trace.jsonl` records the retrieval decision
  (`retrieval` event: strategy/terms/files).

### Task C — Terminal 2's real sandbox/verify: LANDED and verified

`deps.py` auto-resolution picked up `execution.sandbox` +
`execution.verify` the moment they landed — **no code changes needed**.
Verified explicitly:
- `verify(repo_path, target_test, rerun_for_flake_check=1,
  test_command=None, verify_timeout_s=300, *, allow_network=False)` —
  the `test_command`/`verify_timeout_s` kwargs I flagged in the Change
  Log are honored exactly (defaults match; used for both target and
  suite runs, node ids appended).
- `execute_sandboxed` = exact Boundary-1 signature + safe keyword-only
  extras. My e2e suite (real bash through real Docker) passes: 20/20.
- **Deliberate non-change**: Terminal 2 suggests core.py could catch
  `SandboxUnavailableError` and fall back to the subprocess stub. I
  did NOT add silent fallback — "sandboxed must mean sandboxed"; a
  down Docker yields task status "error" (loud, diagnosable, and the
  runtime's crash_retries governs relaunching). Opt-in degraded mode
  can be a future config key if ever wanted.
- Sandbox timeout convention (exit 124 + timed_out=True) matches the
  stub's, so my step-session and feedback logic is unchanged.

## Design decisions worth knowing

- **Agent never touches the original repo.** Every run snapshots the repo
  into `logs/{task_id}/pristine/` + `work/`; diffs are computed between
  those; `restore_dir()` rolls back between attempts. A stale
  `logs/{task_id}/` from a prior non-resume run is archived as
  `{task_id}.old-*`, never deleted; a RESUME relaunch keeps it (Task A).
- **Verifier-gated completion is absolute.** `status="success"` is only
  set when `verify()` confirms target test pass + full-suite regression
  pass + not flaky. `SUBMIT` only ends a step session. If the target
  already passes on the pristine copy, the task short-circuits to success
  with zero model calls (proved by an `ExplodingModel` test).
- **Model defaults are `None`** in `config.DEFAULTS` (model/provider/
  api_key). An unset value means "let the router decide" — Terminal 3's
  adaptive routing depends on this; pin values in `task.config` to force
  a model.
- **Multi-line commands are first-class.** `_extract_command` keeps
  fenced blocks and bare heredocs whole (a beheaded `python - <<EOF`
  heredoc was a real test-caught bug).
- **Constraint re-injection rides every tool result** (end of message =
  max recency): issue one-liner, current step, remaining steps, protected
  paths, "don't modify tests".
- **Failure feedback flows both ways**: step-to-step (intra-attempt) and
  attempt-to-attempt (after rollback). Feedback includes the verifier's
  raw output tail, not just "it failed".
- **Resume granularity is step-level** (state.json's granularity): a
  crash mid-step re-runs that step from its partial edits in work/; a
  crash after a step-completion skips it. `plan.json` (steps +
  in-flight attempt + spent cost) is the harness-internal bookkeeping
  that makes "continue the interrupted attempt" well-defined.

## What's stubbed / mocked, and why

- **All three real boundary modules have landed** (execution.sandbox,
  execution.verify, runtime.model_router, memory.code_graph); `deps.py`
  resolves them automatically. `harness/_stubs/` remains only as the
  import-error fallback (e.g. Docker-less dev machines) and as the
  explicit "want the old behavior" import path per Terminal 2's AGENTS.
- Tests use `tests/fake_model.py` (`ScriptedModel` / `ExplodingModel`)
  injected through `deps.set_call_model` — no network, deterministic,
  real bash + real Docker sandbox + real pytest runs in e2e tests.
  `tests/resume_driver.py` runs the real loop in a child process so
  hard-kill tests don't take pytest down with them.

## Definition of Done — where it stands

Five genuinely different bugs (boundary condition, off-by-one, missing
guard, undefined name, mutable default) in five fixture repos under
`tests/fixtures/bug0{1..5}_*/`. The e2e suite fixes all five with real
bash commands through the full stack (now against the REAL Docker
sandbox), each producing a valid `state.json` (exact Boundary 4 key
order) and complete `trace.jsonl`. Loop behaviors covered by tests:
verifier-gating (model claims SUBMIT without fixing → task FAILS),
retry-recovery (syntax-error attempt → clean fix on attempt 2),
max-retries, budget cap, wall-clock timeout, protected-path rejection
(agent tries editing tests → blocked), original-repo-never-mutated, and
(Round 2) hard-kill → relaunch → resume; non-resume relaunch archives;
corrupt/missing resume state degrades to fresh.

**(Round 4) Definition of Done now ALSO proven on an unfamiliar real
OSS repo** — jaraco/path, a deliberate security-adjacent bug
(in_place() permission loss), full stack incl. real cloud model,
approval gate, git output, rationale, and memory ingestion: SUCCESS in
1 attempt ($0.053, 6 model calls, 373s). The first attempt's honest
failure is part of the record: it exposed and fixed a real harness
defect (editor.py binary/artifact handling — see Round 4 above) that
the 5 controlled fixtures could never catch. Evidence:
`logs/oss-round4/` (driver, validator, oss_run_report.json, both runs'
full logs).

**(Round 6) DoD now proven on FOUR unfamiliar OSS repos total** — the
R4 repo plus parse (flat utility), bottle (single-file framework), and
click (src-layout) — three MORE shape-varied repos each with a genuine
introduced bug, all fixed verifier-gated through the full stack (see
Round 6 Task A for the honest scripted-model account and the endpoint
degradation evidence). Evidence: `logs/oss-round6/multi_repo_report.json`.

**Test status: 150 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py
tests/test_adversarial.py`), including the 4
resume tests, 6 structural-retrieval tests, (Round 3) 2 context-regression
+ 3 git-output/rationale e2e + 2 approval-mode e2e tests, (Round 4) 3
editor binary/artifact regression tests, (Round 5) 26 RECALL unit
tests + 3 new e2e (cross-session RECALL reinjection, RECALL
budget-exhaustion, exhausted-turns success-state), and (Round 6) 43
adversarial tests (protected-path traversal/VCS-forgery, deny-pattern
matrix, sandbox containment, prompt-injection e2e, rename evasion).
All Docker-gated suites per Terminal 2's skip convention.

## Known limitations (honest, accepted)

- **Failure classification is NOT implemented — deliberately.** Retry
  strategy is naive-retry-with-rich-feedback (verifier output tails flow
  step→step and attempt→attempt at the documented `last_feedback` seam).
  The chosen novel mechanism for this project was ADAPTIVE MODEL ROUTING
  (Terminal 3's difficulty prediction — measured: ~2.6-3.5x cheaper at
  equal success), not repair classification; the `last_feedback` seam is
  where spec item 24/25 could plug in if ever revived. Accepted as the
  documented scope boundary, not a silent drop.
- **RECALL matching is substring, not semantic**: a RECALL finds what the
  terms literally mention in the trace (case-insensitive). True synonym
  recall (query "average" for `mean()`) would need embeddings — same
  vocabulary gap as retrieval, same future-work seam.
- **Docker-less dev machines** fall back to the local subprocess sandbox
  stub via deps.py import-error resolution (fail-loud in production: no
  silent unsandboxed fallback when the real sandbox raises
  SandboxUnavailableError).

## What's next (Phase 3 hooks, not started — none are CORE)

- Embedding-based retrieval for true synonym gaps (issue says "average",
  symbol is `mean()`) — subword matching covers identifier decomposition
  but not vocabulary; same seam would upgrade RECALL from substring to
  semantic matching. Would need a local embedding index (ChromaDB
  experience per spec Phase 2). (Would ALSO fix decision-memory query
  recall: the memplan ablation's initial bug04 seed row missed its
  query purely on keyword overlap — see Improvement Round 2 Task B.)
- ~~`query_structure`/`query_decisions` woven into planning~~ — **DONE in
  Improvement Round 2 (2026-09-10)**: the planner queries decision
  memory repo-scoped before planning and injects it as a prompt section;
  measured in a real-model ablation (mistake recurrence 3→0, -23%
  model calls, at equal success). The MCP-wire form is unnecessary
  in-tree (same process tree, programmatic store access); step-session
  weaving remains possible future work if steps ever need mid-task
  memory.
- Failure classification feeding repair strategy (spec item 24/25, the
  documented `last_feedback` seam) — see Known limitations; deliberately
  not the chosen mechanism.

## Known issues / flags for other terminals

- **Terminal 2 (execution):** your verify() kwargs landed exactly as
  requested — closed out, thanks. Not adopting the suggested
  SandboxUnavailableError→stub fallback (fail-loud is the right
  default; see Task C note above). Round 4 note: repos whose test
  setups materialize run artifacts in the repo dir (pytest-cov's
  `.coverage` SQLite, cache dirs) used to poison my diff/files_touched
  — fixed harness-side (editor.py skips them + binary-safe decode);
  no execution-side action needed, but worth knowing the artifact
  class exists beyond semver's symlink quirk.
- **Terminal 3 (runtime):** the resume contract is LIVE — your worker's
  `cfg["resume"]=True` + state.json gate now actually resumes the real
  harness (plan reuse, step skip, attempt continuation, budget seed).
  No runtime-side changes needed. Your 3 scheduler-test failures noted
  in Round 1 were yours (fake-harness-pinned) and untouched by this
  round. Round 4 validation: your scheduler/worker handled a real
  crash-retry-exhaustion loop exactly per contract under a REAL harness
  bug, and the hang-check pinning pattern (`hang_heartbeat_stale_s` ≥
  slow model calls) held with a real cloud model. **Round 5: your
  cli-real-smoke state.json flag is CLOSED** (root cause + fix in Round 5
  Task A2 above; Change Log entry filed) — `harness status` now renders
  true progress on the exhausted-turns-success path.
- **Terminal 4 (memory):** `retrieval.py` now consumes
  `memory.code_graph.CodeGraph` programmatically (load_or_build + raw
  Graph nodes/edges; NOT the string `query()` surface). Contract
  addition logged in INTERFACES.md. `state.json` schema unchanged;
  `decisions` now also records the resume decision. Keep the
  Graph/NodeInfo dataclasses stable-ish or flag in the Change Log.
  Round 4: your recursive poll + MCP query_decisions surface ingested
  and served the OSS run's decisions from the REAL logs tree with zero
  changes — confirmed by query, not assumption.
  **Improvement Round 2 (2026-09-10): the harness is now your store's
  second PROGRAMMATIC consumer** - the planner queries
  open_default_store().search(..., repo_path=task.repo_path) before
  every plan. Keep search's repo_path kwarg + open_default_store stable
  or flag in the Change Log. state.json now writes the ADDITIVE
  repo_path key (your ingest_state_file already read it); the
  decision_memory trace event is safe to surface. The memplan ablation
  pins HARNESS_DECISIONS_DB per-run - your default location convention
  is unchanged.
- **All:** `logs/_code-graph/` is the shared structural index root
  (repo-keyed subdirs); `logs/{task_id}/plan.json` is
  harness-internal (not Boundary 4) — don't parse it from outside the
  harness. Round 5: new `recall` trace events are safe to surface in
  dashboards; the `RECALL` step-prompt doc block does NOT touch the
  planner prompt's `## Issue` / `## Retrieved context` markers your
  difficulty estimator keys on.
