# INTERFACES.md — Contract Between the 4 Parallel Workstreams

This file is the source of truth for how the four modules talk to each other.
Build against these contracts, not against each other's internal code. If a
signature must change, update this file first and note it under "Change Log"
so the other three terminals see it.

Repo layout:
```
repo/
  AGENTS.md
  INTERFACES.md
  project-spec.md
  harness/        <- Terminal 1
  execution/      <- Terminal 2
  runtime/        <- Terminal 3
  memory/         <- Terminal 4
  mcp_server/     <- Terminal 4
  cli/            <- Terminal 4
  tests/
  logs/           <- gitignored; structured state files + full traces
```

## Shared data types (all modules import these — put in `shared/types.py`)

```python
from dataclasses import dataclass, field
from typing import Literal, Optional

@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

@dataclass
class VerificationResult:
    target_test_passed: bool
    baseline_passed: bool          # did this test pass BEFORE any edit?
    regression_passed: bool        # did the full suite still pass after?
    flaky: bool                    # same test, different outcomes across reruns
    raw_output: str

@dataclass
class TaskResult:
    task_id: str
    status: Literal["success", "failed", "error", "timeout"]
    attempts: int
    diff: Optional[str]
    verification: Optional[VerificationResult]
    cost_usd: float
    model_calls: list              # list of {step, model, provider, tokens, cost}
    log_path: str                  # path to full structured trace for this task

@dataclass
class Task:
    task_id: str
    repo_path: str
    issue_text: str
    config: dict                   # max_retries, budget_cap, etc.
```

## Boundary 1 — Terminal 1 (harness) calls Terminal 2 (execution)

```python
# execution/sandbox.py — implemented by Terminal 2, called by Terminal 1
def execute_sandboxed(repo_path: str, command: str, timeout_s: int = 120) -> ExecutionResult: ...
    # Keyword-only extras (Terminal 2's real implementation, all default-safe):
    #   allow_network: bool = False      # drop --network none for this call
    #   env: dict | None = None          # extra container env vars
    #   mem_limit / cpu_limit / pids_limit  # resource-limit overrides

# execution/verify.py — implemented by Terminal 2, called by Terminal 1
def verify(repo_path: str, target_test: str, rerun_for_flake_check: int = 1) -> VerificationResult: ...
    # Keyword args the real implementation accepts (matches the stub phase):
    #   test_command: str | None = None     # None = autodetect pytest
    #   verify_timeout_s: int = 300
    #   allow_network: bool = False          # keyword-only
```
**Terminal 2's real implementation is LIVE** (Docker sandbox). Semantics the
harness relies on (confirmed by its test suite + a real-repo DoD run):
- `execute_sandboxed` runs each command in a FRESH container per call, repo
  bind-mounted READ-WRITE at /workspace (agent edits persist to the host —
  the harness diffs pristine/work dirs on the host), resource limits
  (mem/cpu/pids), read-only rootfs, cap-drop ALL, no network unless
  `allow_network=True`. Raises `SandboxUnavailableError` when Docker is
  down — it NEVER silently runs unsandboxed (keep the stub for that).
- `verify()` is a STATELESS evaluator of one repo state: runs the target
  test `max(1, rerun_for_flake_check)` times (flaky = differing outcomes
  across reruns; a timeout counts as a distinct outcome), then the full
  suite for `regression_passed`. `baseline_passed` is ALWAYS False in the
  returned result — only the harness knows the pristine outcome (it calls
  verify() on the pristine copy and fills the field itself).
- Deps: per-repo images built lazily (fingerprint = dep manifests only,
  so code edits never trigger rebuilds) from requirements.txt and
  pyproject [project] dependencies. The repo's own package is NEVER
  installed into the image — tests always exercise the bind-mounted
  source (installed copies would shadow the agent's edits).
- The old stub-phase guidance (build against harness/_stubs) still holds
  for anyone wanting a no-Docker fallback; deps.py resolves real-first.

## Boundary 2 — Terminal 1 (harness) calls Terminal 3 (runtime/model router)

```python
# runtime/model_router.py — implemented by Terminal 3, called by Terminal 1
def call_model(
    messages: list,
    difficulty_hint: Optional[str] = None,   # "easy" | "hard" | None — used by adaptive routing
    provider: Optional[str] = None,          # override; None = let router decide
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str: ...
```
**Until Terminal 3's router exists**, Terminal 1 should build against a stub that just calls
one hardcoded provider directly with the same signature.

## Boundary 3 — Terminal 3 (runtime) calls Terminal 1 (harness)

```python
# harness/core.py — implemented by Terminal 1, called by Terminal 3's scheduler
def run_task(task: Task) -> TaskResult: ...
```
This is THE most important contract in the whole project — the scheduler's entire job is
calling this function many times concurrently with checkpointing around it. Terminal 3 can
build and test its scheduler against a fake `run_task` (sleep + random pass/fail) before
Terminal 1's real one is ready.

## Boundary 4 — Terminal 4 (memory/MCP) reads from Terminal 1's structured state

```python
# harness/context.py — implemented by Terminal 1; format consumed by Terminal 4
# Structured state file written per-task at logs/{task_id}/state.json:
{
  "task_id": str,
  "plan": [str, ...],
  "completed_steps": [str, ...],
  "files_touched": [str, ...],
  "decisions": [str, ...],        # e.g. "chose full-file rewrite over diff for this file"
  "remaining_plan": [str, ...],
  # ADDITIVE keys (written AFTER the six above, only when set —
  # consumers MUST treat extra keys as ignorable):
  "repo_path": str,               # the task's repo (memory scopes decisions by it)
  "change_groups": {              # Improvement Round 2: declared ATOMIC multi-file
    "<group-name>": ["file1.py", "file2.py", ...]  # change units from the plan —
  }                                # validated together, rolled back together
}
```
Terminal 4's decision/pattern memory ingests the `decisions` field across tasks over time.
Terminal 1 owns the schema — if it changes, update this file.
The six-key prefix order is the stable contract; `repo_path` and
`change_groups` are additive and absent when unset (single-file fixes
write neither, or only repo_path).

## Boundary 5 — Terminal 4 exposes memory to everyone via MCP

```python
# mcp_server/server.py — implemented by Terminal 4
# MCP tools exposed:
#   query_structure(query: str) -> str      # code graph lookups ("what calls X")
#   query_decisions(query: str) -> str      # decision/pattern memory
#   record_decision(text: str) -> None      # called by Terminal 1 after each task
```
Any module (or an external MCP client) can call these once the server is running locally.

**Consumption note (Round 2, memory-informed planning):** Terminal 1's
PLANNER now actively queries this memory surface before planning — see the
Change Log entry dated 2026-09-10. It calls
`memory.decision_store.open_default_store()` programmatically (NOT the MCP
wire — same process tree, no server round-trip needed) and
`DecisionStore.search(query, limit, repo_path=...)` with the task's repo
for scoping. Terminal 4: keep `search`'s repo_path kwarg and
`open_default_store` stable, or flag here.

## Boundary 6 — Terminal 4's CLI calls into Terminal 3's scheduler and Terminal 1's harness

```python
# cli/main.py — implemented by Terminal 4
# harness fix --repo <path> --issue <text> --model <name>   -> calls harness.core.run_task directly
# harness run-benchmark --subset <name> --concurrency <n>   -> calls runtime.scheduler.run(...)
# harness status --task-id <id>                             -> reads logs/{task_id}/state.json
```

## Change Log
- 2026-09-22 (slash-surface round): **9 new built-in slash commands,
  REPL + TUI; BUILTIN_SLASH_COMMANDS grows accordingly (custom names
  can no longer shadow /init /model /login /logout /mcp /skills /cost
  /undo /clear — /model was also missing from the set).** New shared
  helpers in cli.interactive (both shells render through them):
  trace_usage_sum / session_spend_total, _render_cost/_render_skills/
  _render_mcp/mcp_server_table, _do_init (calls vexconfig writers),
  _do_logout (calls onboard.cmd_logout), _do_clear, undo_result (+
  history_matches reusing session_matches for /history). /login calls
  the onboarding wizard (REPL: cmd_login; TUI: _OnboardScreen modal);
  /mcp resolves labels via the agent loop's read-only
  _resolve_mcp_server and lists tools via memory.mcp_client
  (harness/agent_loop.py itself untouched — surface names only).
  No harness/runtime/memory contract changes; trace/state schemas
  unchanged. Tests: tests/test_cli_slash2.py (33).
- 2026-09-21 (multi-language round): **Boundary 1 now covers JS/TS —
  no signature changes, language is auto-detected.** `verify()` runs
  Jest/Vitest repos (target filter `<file> -t <name>`; baseline/
  regression/flake semantics unchanged and language-independent);
  `execute_sandboxed` builds node:22-slim images with npm deps on a
  read-only volume mount (Python image path byte-identical).
  `memory.code_graph` indexes .js/.jsx/.mjs/.cjs/.ts/.tsx via
  tree-sitter (same Graph/NodeInfo shapes — harness retrieval needs
  no changes). Tests: tests/test_verify_js.py + test_multilang_graph.py.
  SCOPE FLAG: project-spec.md "explicitly out of scope" still says
  multi-language support beyond Python — that line is stale as of this
  round and needs an amendment; not silently rewritten here.
- 2026-09-21 (agent demo-parity round): **agent resume replays history
  (not a restart); _resume_task returns its result; run_agent gains an
  additive resume_history kwarg.** `harness.agent_loop.run_agent(...,
  resume_history=None)` injects prior-session context as a steering
  user message (at=agent-resume-history) and emits the same steering
  trace kind the plan-guidance path uses; new
  `load_resume_history(task_id, log_root)` rebuilds the preamble from
  the prior trace (request + files + recent exchanges, never raises).
  `cli.interactive._resume_task` returns the run's result dict (fix
  path: _execute_task's; agent path: same task id, pristine/orig kept,
  prior trace + conversation turns replayed) and both REPL /resume
  branches fold it via `_fold_resumed` into `last` + the conversation
  transcript. `_run_one_agent` gains the additive `resume_history`
  kwarg (legacy fakes without it fall back without replay). TUI:
  /diff recomputes the live agent diff when last[] is empty (REPL
  parity), _start_resume records the /resume user turn, and the
  resume worker folds the returned dict via _note_result. New
  `demo/agent_demo.py` (offline scripted 7-step demo). No fix-mode
  contract changes (core/editor/tools/verify/sandbox untouched).
- 2026-09-21 (first-run onboarding round): **no model set + `vex` ->
  inline wizard (once), saves, never asks again. CLI-internal only,
  no Boundary signature changes (fix-mode run_task/verify/sandbox/
  state.json untouched; the harness never sees the wizard).**
  New `cli/onboard.py`: PRESETS (Official OpenAI/Anthropic/Gemini via
  litellm names + Router OpenRouter/TokenRouter/Ollama/Custom with
  free-text base_url + model name — never a hardcoded-only provider
  list), detection over the effective resolution (flags > env >
  local > project > global > legacy; local loopback endpoints need
  no key), `run_repl_wizard` (pick -> editable base_url -> model
  with suggestions + free text -> masked api_key -> ONE tiny live
  litellm call; fail = error + retry, bad creds never saved), save
  discipline (api_key+base_url ALWAYS global; model global unless
  --tier project; official picks clear stale router bases),
  `vex login [--tier]` / `vex logout` (strip key, keep model/base),
  `/model` display (+ source tier) in REPL and TUI, flag-command
  gate (missing creds -> honest stderr + exit 4, never prompts;
  --json emits a parseable error doc; fake/mock/scripted models
  exempt). `cli/tui.py`: `_OnboardScreen` modal (same stepped flow;
  test call off the UI thread; Esc skips) auto-pushed once at
  session start via an explicit `onboard_prompt` flag from run_tui
  (a mount-time isatty probe fires under Pilot — textual swaps
  sys.stdout — so direct VexApp(...) construction stays modal-free).
  `cli/vexconfig.py`: `set_tier_key` refuses project-tier api_key
  (would be committed), settings writes chmod 600 best-effort
  POSIX. `cli/interactive.py`: REPL session-start hook (once;
  pipe-safe) + `/model`. `cli/main.py`: `login`/`logout`
  subcommands; `fix` + `run-benchmark` gates. Skips: `/skip`,
  VEX_NO_ONBOARD=1, non-TTY, --json. Tests:
  tests/test_cli_onboard.py (detection, save tiers, masking,
  wizard incl. wrong-key-saves-nothing + retry-fix, gates incl.
  --json doc, login/logout, TUI modal incl. full Pilot save flow);
  test_cli_errors crash pin + test_cli_adversarial injection pin
  updated for the exit-4 contract (creds supplied / 4 allowed —
  intents unchanged). Known environmental note: Docker daemon down
  machine-wide at round time — Docker-backed e2e (offline fix,
  JsonMode, scripted interactive) fail at baseline verify before
  any touched code runs (trace-proven); re-run when back.
- 2026-09-21 (agent trust round): **the agent loop is daily-driver safe:
  TUI approval modal, agent plan preview, undo hardening, MCP/plugin/fetch
  tools. No Boundary signature changes (fix-mode run_task/verify/sandbox/
  state.json untouched; `vex fix` path byte-identical); additive
  harness-internal + CLI-internal surface only.** `harness/agent_loop.py`:
  new tools `fetch` (JSON + FETCH line; GET-only SSRF-guarded webfetch,
  `agent_fetch_enabled`/`agent_max_fetches`, budget exhaustion nudge —
  BASH with a FETCH-shaped command is redirected, never shelled),
  `mcp`/`mcp_call` (installed plugin servers + config
  `agent_mcp_servers`; failures degrade to TOOL ERROR kinds, never a
  traceback), plugin verbs via one `route_tool` (builtin|mcp|plugin|
  unknown; read-only verb shapes auto-run even in require mode,
  non-conforming shapes rejected, unknown tools honest errors);
  `parse_tool_call` accepts plugin first-tokens via optional
  `known_verbs` (default strict — the `{"tool":"nuke"}` pin holds);
  `render_agent_plan` (heuristic steps+files, no verifier fabrications;
  approved plans inject as `plan_guidance` steering, never a contract);
  BASH runs LIVE (local stub, never Docker; respects injected fakes;
  deny-guard + cwd tracking kept; Ctrl+C stops the call, not the
  session); `undo_edits` gains per-file `targets=` + appends an `undo`
  trace event (pristine/ never touched; resume keeps pristine/orig so
  undo stays coherent). New config keys (all additive):
  agent_fetch_enabled, agent_max_fetches, agent_live_bash,
  agent_mcp_servers. `cli/interactive.py`: `_run_one_agent` gains
  `approve_fn_override` + `plan_guidance`; `/plan <text>` classifies
  (agent-shaped -> lightweight preview with approve/edit-steer/cancel,
  else the fix-loop preview); `/diff undo <file|all>` restores exactly
  that file; REPL approver unchanged (per-call) + VEX_NOTIFY bell.
  `cli/tui.py`: `_agent_approve_fn` (same _ConfirmScreen pattern:
  diff+command body, y=once / a=always-latched / n=safe-default,
  VEX_NOTIFY bell); `_agent_worker` passes it + pending guidance
  (signature-inspected so legacy test fakes keep working); `/plan`
  agent preview with approve/edit modals; `/diff undo <file|all>`.
  `cli/tracelog.py`: FETCH/MCP feed labels (additive; fix loop never
  emits them). Tests: test_agent_loop 56 (plan/fetch/MCP/plugin/undo/
  live-bash/REPL-preview), test_cli_tui 47 (+4 approval-latch unit);
  live drive (require approve+deny+undo+diff, plan->edit->DONE, real
  `python -m mcp_server` query_decisions through the loop); evals
  --check OK; ruff clean on new/edited regions (interactive/tui held
  at pre-existing baselines).
- 2026-09-21 (first-run .vex/ scaffold round): **first `vex` in a repo
  auto-scaffolds the project layout; `vex config init-project` is the
  explicit form of the same scaffold. CLI-internal only, no Boundary
  signature changes.** `cli/vexconfig.py`: new `find_git_root`
  (nearest `.git` ancestor; the outside-a-repo gate), new
  `ensure_project_layout` (creates settings.toml + key-less
  settings.local.toml + `commands/fix.md` + `skills/code-review/
  SKILL.md` examples — only missing pieces, never overwrites; example
  dirs are created only when the dir itself is new, so a deleted
  example stays deleted), `ensure_project` keeps its
  `(created, path)` signature and now scaffolds the full layout, new
  `maybe_scaffold_repo` (git-root detection with $VEX_PROJECT_DIR
  override; best-effort, never raises — the session entry points call
  it after `ensure_first_run`: REPL in cli/interactive.py, TUI in
  cli/tui.py, both with a one-line `repo setup:` notice only when
  something was created). Broken-TOML/unreadable/wrong-type warnings
  are now once-per-process per file (`_warn_once` — `config list`
  re-reads the chain per key and used to spam one warning per key).
  `vex config init-project` (cli/main.py) reports the extra scaffolded
  files. Installers verified package-only (no config writes; banners
  already read "the AI coding agent for your terminal"; python>=3.10
  hard-fail, git/Docker warn-only on the PyPI path, idempotent PATH,
  post-install `vex --version` + `vex update --check`). Tests: 10 new
  `TestProjectScaffold` in tests/test_cli_config.py (52/52 file
  green), incl. a real-`git status --untracked-files=all` pin
  (settings.toml visible, settings.local.toml ignored). Session-entry
  drivers in tests/test_cli_vex3.py + tests/test_modes.py now
  `monkeypatch.chdir(tmp_path)` so they can never scaffold the real
  tree. Known edge: walk-up repo detection treats a home-dir dotfiles
  git repo as a repo (consistent with the existing project-tier
  walk-up); $VEX_PROJECT_DIR overrides detection.
- 2026-09-21 (general-agent round): **interactive `vex` is now a general
  coding agent (ONE live-repo loop); `vex fix` UNCHANGED. No Boundary
  signature changes (fix-mode run_task/verify/sandbox/state.json all
  byte-identical); new harness-internal + CLI-internal surface only.**
  New module `harness/agent_loop.py`: `classify_agent_input` (question |
  agent_task | chit_chat — deterministic rules + ONE cheap model call
  for the gray zone, same tier discipline as harness.intent),
  `run_agent` (generic READ/GLOB/GREP/BASH/EDIT/WRITE/MEMORY/VERIFY/
  DONE tool loop on the LIVE repo via the existing
  execute_sandboxed/BashSession + editor + decision-memory surfaces;
  steering journal polled every turn; NO verifier gate unless
  target_test/test_command declared; pristine/ snapshot + orig/ per-file
  originals under logs/{task_id}/ are the diff/undo reference only),
  `parse_tool_call` (JSON or one-line form), `agent_diff`/`undo_edits`.
  New config keys (all additive): agent_max_turns (25),
  agent_approval ("auto"; "require" gates BASH/EDIT/WRITE behind
  approve_fn), agent_context_files/lines, agent_max_read_chars,
  agent_intent_enabled. Session dispatch (cli/interactive.py +
  cli/tui.py) uses the agent dispatcher: questions -> the unchanged
  read-only _run_one_question; everything else work-shaped ->
  _run_one_agent (live repo, diff + /diff undo, steering registration,
  session index). Legacy `_run_one_fix/_run_one_build/_run_one_research`
  + `vex fix` + harness.router/intent stay for the benchmark path
  (untouched); /plan + /review-with-template intentionally stay on the
  fix loop (preview machinery lives in _execute_task). FeedBuilder
  renders the new tool_call verbs + edit_applied/approval events
  (additive). Tests: tests/test_agent_loop.py (32); session-wiring
  suites updated to the 3-way contract (test_modes TestSessionWiring,
  test_cli_vex3 bug-sentence, test_cli_tui dual-fake + agent dispatch +
  direct fix-worker preview tests, test_cli_plugins generic-custom-
  command). Known limits: TUI agent_approval=require has no modal yet
  (tools refused honestly); research-shaped input is answered via the
  repo-grounded question path (no web FETCH there — use the legacy
  research entry programmatically).
- 2026-09-21 (agent-session round): **persistent conversation sessions;
  CLI-internal only, no Boundary changes.** New module `cli/session.py`
  (one state file per conversation under `<log_root>/_conversations/`:
  transcript turns + input history + compacted summary; `@path`
  expansion; `compact_session` reusing the existing
  `TraceLogger.find_events` recall primitive; memory-first
  `session_memory_brief`/`ingest_session_facts`; best-effort
  `copy_text_to_clipboard`). `cli/commands.py::BUILTIN_SLASH_COMMANDS`
  gains `/plan`, `/compact`, `/copy-diff`, `/copy`, `/trace`, `/feed`,
  `/steer` (new builtins a custom command can no longer shadow);
  `/review` deliberately stays OUT so project/plugin `review.md`
  templates keep working (bare `/review` renders diff + rationale,
  `/review <args>` with a template runs it). `record_session`
  (cli/interactive.py) now also ingests facts into memory. New
  interactive commands in both REPL and TUI: `/plan`, `/review`,
  `/compact`, `/copy-diff`, `/resume` with no id (most recent
  resumable). NOTE (parallel session, same tree): the agent-loop
  migration (`harness.agent_loop`, `_run_one_agent`) is in flight;
  its stale `test_slash_custom_command_without_arguments` pin
  (monkeypatches `_run_one_fix`, code now calls `_run_one_agent`)
  fails independent of this round.
- 2026-09-21 (packaging/installer round — v0.2.0): **PyPI-first
  installs; one behavior change in cli/selfupdate.install_source().**
  `install_source()` now returns the PyPI spec `vex-harness` by
  default; a git URL only when `VEX_INSTALL_SOURCE` is set or
  `VEX_INSTALL_REPO`/`VEX_INSTALL_REF` is explicitly set (previously
  always `git+https://github.com/<repo>@<ref>`). `vex update` for
  pip/venv methods therefore runs `pip install --upgrade vex-harness`
  instead of the git URL; pipx (`pipx upgrade vex-harness`) and the
  source-checkout refusal are unchanged. `latest_available_version()`
  now checks PyPI JSON first, GitHub tags as fallback (order swapped).
  The three installers (install.sh/.ps1/.cmd) default to the same PyPI
  spec with the same env-var git fallback; banners read "the AI coding
  agent for your terminal"; Docker is warn-only, git required only for
  git-URL sources; post-install runs `vex update --check`
  (best-effort). pyproject 0.2.0 gains `pygments>=2.13` and
  `tomli>=2.0; python_version<'3.11'` deps + `[tool.setuptools.
  package-data] cli = ["fixtures/**/*"]` (smoke fixture now ships in
  the wheel). No Boundary signatures changed; `vex --version` still
  pinned to pyproject by tests/test_cli_release.py.
- 2026-09-14 (Terminal 1+4 joint, mid-task steering round): **new
  mid-task steering surface; NO Boundary changes (purely additive
  harness-internal + CLI-internal surface).** New module
  `harness/steering.py` — the transport is the append-only journal
  `logs/{task_id}/steering.jsonl` ({"op": "inject"|"consume", ...}, one
  JSON line each; the loop's SteeringBuffer RE-SCANS the journal on
  every poll so ANY process — REPL reader thread, TUI, a second
  terminal, a test driver, an MCP client — can inject by appending).
  Loop consume points (harness-internal, in core.py): turn boundaries
  (guide → USER STEERING message in the live session; strong intents
  yield the step), step boundaries (abort → clean resumable stop;
  replan → shared re-plan path keeping work/), the final gate, and a
  pre-mint re-check — pending steering at ANY success point blocks
  success minting (verifier-gated completion never shortcut; the fix
  is re-verified after incorporation). Steering consumes as
  NON-transition state-machine events (`record_event`) — replan rides
  existing editing→repairing→planning edges; abort lands failed. The
  journal replays on construction: an unconsumed inject stays pending
  across a crash/resume (the resume contract is unaffected). New
  additive config keys: `steering_enabled` (True; False = the OFF arm,
  buffer never constructed), `max_pending_steering` (16; over-cap
  injects refused honestly), `steering_max_chars` (4000). New
  trace kinds (additive, same {ts,kind,data} schema): `steering`,
  `steering_abort`, `steering_replan`, `steering_step_yield`,
  `plan_replaced` (a steering re-plan's replacement). CLI surface
  (Terminal 4, cli/interactive.py + cli/tui.py): plain text while a
  run is live steers it (REPL via the `_ReplReader` stdin-owner
  thread + `_LIVE_RUN` registration around every `_execute_task`/
  `_run_one_build` call; TUI via in-flight plain text + `/steer`);
  `steer_live_run(line, task_id, log_root, source, say)` is the
  public inject helper (intent parsed at inject time:
  guide/replan/abort; conversational input never injected). Tests:
  tests/test_steering.py 41/41 (incl. Docker-gated e2e through the
  real loop), tests/test_cli_tui.py 33/33.
- 2026-09-14 (Terminal 1, proactive codebase health scan round): **new
  read-only scan mode + Task-C handoff; NO Boundary changes.** New
  module `harness/scan_mode.py` with the public entry
  `run_scan(repo_path, config=None, log_root=None, task_id=None,
  remote=None, focus=None, max_findings=None) -> dict` (status
  "success"|"error", scan_id, ranked findings with `index` stamped,
  shown/suppressed, notes, counts, report/scan/trace paths — read-only:
  no shell/sandbox/model; findings from the code graph + stdlib AST +
  dependency manifests, deterministic ranking, no invention).
  Consumers of existing Boundaries: coverage detection consumes
  memory.code_graph via harness.deps (load_or_build with the index
  root OUTSIDE the scanned repo); finding resolution consumes
  memory.paths.safe_task_dir for scan-id validation. New additive
  config keys: `scan_max_findings` (8), `scan_remote_deps` (False),
  `scan_pypi_timeout_s` (10), `scan_smells_per_kind` (3),
  `scan_func_gap_max` (3). New artifacts (harness-owned, inside the
  logs root): `logs/scan-<id>/scan.json` (ALL ranked findings; the
  report shows the top slice), `report.md`, `trace.jsonl` (events:
  task_start(mode=scan), scan_detector×3, scan_summary, task_end).
  New CLI surface (additive): `vex scan --repo <path> [--focus
  coverage|smells|dependencies] [--remote] [--max-findings N] [--json]
  [--fix N]` and `vex fix --finding <scan_id>#<n>` (also `vex scan
  --fix N`) — resolves a finding from scan.json and dispatches through
  the EXISTING verifier-gated entries (plain fix loop with a
  not-yet-existing target test / build mode for version-floor
  dependency bumps). T3 (runtime): no scheduler changes — a scan runs
  in-process, no workers; T4 (memory): scan.json/trace.jsonl are
  harness-internal like plan.json — no ingest changes needed. Tests:
  tests/test_scan_mode.py 50/50 (offline + mocked-PyPI remote +
  hostile-ref containment + Docker-gated e2e through the real
  run_task). Validation: full-repo run against this repo — 7 focused
  findings in 122.8s, all spot-checked genuine (the noise-budget
  iteration history: 1323 raw smell sites → capped to 3/kind).
- 2026-09-14 (Terminal 3, cross-task learning round): **new offline
  analysis job + difficulty-predictor recalibration loop; NO Boundary
  changes.** New module `runtime/analyze_history.py` (pure CONSUMER of
  the documented formats — trace.jsonl task_start/task_end/result/
  attempt_start/retrieval, `{task_id}.runtime/model_ledger.jsonl`,
  ablation summary.json; reads, never writes them) producing
  `logs/analyze-history/<ts>/report.json`: predictor-vs-outcome
  divergence (routed tasks only), retrieval-strategy-vs-repairs
  association, failure patterns, and a refit of `score_to_hint`'s
  (easy_max, hard_min) band family on a deterministic grouped per-bug
  train split with an honest held-out before/after. New CLI subcommand
  `vex analyze-history [--log-root] [--holdout-frac] [--json]
  [--apply]` (cli/main.py, additive; exit 0/2). ONE small opt-in hook
  in `runtime/difficulty.py`: `score_to_hint` now checks for
  `runtime/difficulty_calibration.json` (written ONLY by the gated
  `--apply` path when held-out accuracy actually improved); absent/
  malformed/out-of-range file = the built-in v2 bands, byte-identical
  behavior (all 8 pre-existing difficulty tests pass unmodified).
  First real run's honest verdict: recalibration MARGINAL (held-out
  0.75 → 0.75), nothing applied — details in runtime/AGENTS.md.
  Terminal 1 note: no prompt/trace-format changes are needed by this
  job, but it now READS your trace's task_start config + task_end
  status/attempts/mode fields and the attempt numbering (1-based) —
  if you ever change those shapes, this is a consumer (plus the
  dashboard, which already reads them).
- 2026-09-14 (Terminal 1, long-horizon planning for build mode): **build
  mode gains a multi-session PROJECT layer above the unchanged
  single-session build: new module `harness/build_plan.py` +
  `run_project(request_text, repo_path, config, log_root, project_id)
  -> dict` (status "success"|"checkpointed"|"already_exists"|"error"|
  forwarded sub-task failure). No existing boundary signature changed;
  `harness.build_mode.run_build` is consumed UNCHANGED as the per-sub-task
  engine (one sub-task = one run_build session on the accumulated tree).
  New artifacts (harness-owned, inside the logs root): `logs/{project_id}/
  project.json` — the PROJECT PLAN (criteria, sub_tasks, completed ids,
  current_tree, sessions, status; atomic tmp+replace writes, the
  state.json discipline) — plus pinned accumulated trees
  `logs/{project_id}.tree-s<N>/` (the verified work/ of sub-task N,
  which sub-task N+1 starts from) and per-sub-task build dirs
  `logs/{project_id}-s<N>[.base]`. New config keys (all additive):
  `build_project` (False — routes large build requests through
  harness.router to run_project instead of run_build),
  `project_max_sub_tasks` (4), `project_sub_tasks_per_session` (1 —
  the session budget on BUILDS; a sub-task that completes
  by-verification consumes none; reaching it CHECKPOINTS: status
  "checkpointed", resume with `project_resume=True` + the same
  project_id),
  `project_criteria_max` (8), `project_resume` (False). New trace
  events (logs/{project_id}/trace.jsonl): project_start,
  project_criteria_extracted/capped/parse_error/empty_reply_retry,
  project_plan_generated/capped/parse_error/empty_reply_retry,
  project_plan_saved, project_sub_task_start/end/already_passing,
  project_checkpoint, project_final_verify, project_end — safe to
  surface in dashboards. T4 (memory): project.json is harness-internal
  bookkeeping like plan.json — decisions still flow through the
  per-sub-task state.json files your ingest already reads. Tests:
  tests/test_build_plan.py (20 offline) + tests/test_build_plan_e2e.py
  (3 Docker-gated: the 2-session proof with checkpoint/resume across 3
  distinct module changes, failed-sub-task retry-from-checkpoint,
  coverage-gap abort) + the modes/e2e/evals sweeps green.
- 2026-09-14 (CLI session, live agent-trace feed round): **the full-screen
  TUI now surfaces the run LIVE — a readable one-liner-per-action feed
  (reasoning summaries, tool calls, inline diffs), built as a READ-ONLY
  VIEW over the existing trace.jsonl; no boundary signatures changed, no
  new logging path, the trace schema is untouched and the harness stays
  the single source of truth.** New module `cli/tracelog.py`:
  `FeedBuilder.consume(event) -> [FeedEntry]` (pure mapping over the
  PUBLIC trace-event kinds — same schema LiveMonitor consumes;
  `FeedEntry{summary, category, detail, detail_title, index}` carries
  the raw command+output / whole model reply as expandable detail),
  `classify_command(cmd)` (bash -> "Reading x.py"/"Running: pytest …"/
  "Editing x" texture), `summarize_reply(step, content)` (the one-line
  visible-thinking; pure-command replies produce no line), and
  `live_diff(pristine_dir, work_dir)` (inline diff from the SAME
  logs/{task_id}/pristine vs work trees the harness diffs at
  completion — same junk-skip + binary rules). The TUI's trace-tail
  thread feeds each event through the builder and renders entries the
  moment they land (verified live: <2s lag per line); edit-shaped
  entries are followed by the inline diff; `/trace` (TUI modal +
  REPL plain print) lists/expands entries and rebuilds the feed
  POST-RUN from the run's own trace.jsonl (no copy kept). The feed is
  gated on session `state["feed"]` (NOT quiet — the TUI worker sets
  quiet to silence the backend spinner); `/quiet` toggles both.
  T1 (harness): nothing needed — trace.jsonl was already the public
  observability surface; this round only READS it. Tests:
  test_cli_tracelog.py (67) + test_cli_tui.py (33, incl. 4 new) + the
  full CLI sweep (347) + a live e2e through the REAL harness loop +
  REAL Docker sandbox/verify (23/23, report at
  logs/live-feed-e2e/live_feed_report.json).
- 2026-09-14 (CLI session, full-screen TUI round): **`vex` with no
  args now launches a persistent full-screen textual App (cli/tui.py)
  — the rich-print REPL becomes the FALLBACK (no TTY / VEX_TUI=0 /
  textual absent), not the primary. No boundary signatures changed;
  the harness loop, trace schema, and approval protocol are untouched
  (the TUI renders the SAME cli.interactive backend).** New surfaces:
  (a) three embedded-UI hooks in cli.interactive — `_ON_TASK_START(task_id)`
  (fired when a task's trace dir exists; the TUI attaches its live
  run-line), `_CANCEL_RUN()` (the TUI redirects /cancel at its worker
  thread via async KeyboardInterrupt instead of SIGINT-at-main),
  `_PROMPT_BODY(prompt, body_lines)` (the backend declares the plan
  steps / diff that a blocking prompt refers to; the TUI shows them in
  the modal body). All optional; the REPL leaves them None and every
  fire is try/except'd — a broken UI hook can never take a run down.
  (b) `cli.ui.JOKES` + `ui.joke_at(i)` + `ui.THINK_FRAMES` — the
  thinking-phase treatment (distinct orbit glyph + rotating technical
  joke, Qwen-Code-style) shared by the TUI run-line and the REPL
  LiveMonitor. Dependency added: `textual>=0.40` (pyproject).
  Other terminals: nothing to ingest; `python -m cli` / `vex fix`
  flag flows are unchanged, and non-TTY no-args still prints argparse
  usage + exit 2 (CI-safe, tested).
- 2026-09-14 (Terminal 4, CLI citizenship round — release readiness):
  **EXIT-CODE CONTRACT WIDENED (Task B) + three new CLI subcommands;
  no boundary signatures changed.** The documented CLI exit codes grow
  from {0 ok, 1 task failure, 2 usage error} to
  **{0, 1, 2, 3 environment error (Docker/sandbox/deps), 4 model/
  network error, 130 interrupted}** — 0/1/2 semantics byte-identical,
  3/4 split the old catch-all 1 so CI can distinguish "fix the
  machine" from "the bug beat the agent". Source of truth:
  `cli/exit_codes.py` (EXIT_CODES table; `classify_exit_code(exc)`
  over the same layer mapping `cli.errors` documents — never raises).
  Callers that check `rc == 1` for everything keep working EXCEPT the
  env/model classes; if any script needs the old collapse, it can map
  `rc in (1,3,4)` -> failure. New subcommands (all additive, argparse):
  `vex update [--check]` (cli/selfupdate.py — install-method detection
  pipx/venv/pip/source; source checkouts get a git-pull recipe, never
  a fake upgrade), `vex completion <shell> [--install]` + the hidden
  dynamic `vex __completions` backend (cli/completion.py — candidates
  derived from the real parser, so they can't drift from --help), and
  `vex uninstall [--yes|--dry-run]` (cli/uninstall.py — enumerates
  installer-created venv/shims/PATH/config roots, confirms, removes;
  Windows user-PATH edits go through the registry API, same as
  install.ps1). `vex fix --json` / `vex status --json` (machine-
  readable stdout documents; no other output on stdout). T3/T1: no
  action needed — TaskResult, run_task, scheduler contracts untouched;
  the category split lives entirely at the CLI boundary. Tests:
  tests/test_cli_release.py (49) + updated test_cli_errors pins
  (SandboxUnavailable -> 3, unmapped -> still 1).
- 2026-09-14 (Terminal 1, Modes round — intent router & multi-mode
  capability): **NEW mode-handling contracts (Tasks A-F). All additive;
  fix-mode `run_task` is UNCHANGED — the new modes are entries AROUND
  it, never loop variants; state.json schema gains one additive key
  (`mode`, omitted for fix tasks).** The project's top-level story
  changes: a general coding agent with a rigorously benchmarked fix
  mode, not just a bug-fixing harness (see HANDOFF.md).
  - **Task A — `harness.intent`**: `classify_input(text, config,
    trace=None) -> Intent(kind: "fix"|"build"|"question"|"research"|
    "convo"|"ambiguous", reason, reply, used_model)` and
    `classify_deterministic(text) -> Intent` (offline tier). Two-tier:
    deterministic rules for clear cases (never touches the model — `hi`
    is free and never launches a task); ONE cheap model call
    (`difficulty_hint="easy"`; pin via `intent_model`) for the gray
    zone; ANY model failure/unparseable reply degrades to `ambiguous`
    (the session asks, never guesses). `intent_enabled=False` = legacy
    everything-is-fix. Config keys: `intent_enabled` (True),
    `intent_model` (None).
  - **Task B — `harness.router`**: `route(text, repo_path, config,
    log_root=None, trace=None, handlers=None) -> ModeResult` (dict:
    mode, status "success"|"failed"|"error"|"already_exists"|"reply",
    answer/result/task_id/trace_path) and `route_kind(text, config) ->
    Intent` (the single import site for the routing decision). fix →
    `core.run_task` (unchanged); question/build/research → their
    handlers; convo/ambiguous → `status="reply"` with `answer=` for the
    SESSION to print — routing never launches anything for them. The
    `handlers=` kwarg overrides dispatch (tests inject fakes).
  - **Task C — `harness.qa_mode`**: `run_question(question, repo_path,
    config, log_root, task_id) -> {answer, status, files, cost_usd,
    model_calls, task_id, trace_path}` — read-only (retrieval + decision
    memory on the ORIGINAL repo; no sandbox, no pristine/work). The
    model may emit `READ <repo-relative path>` control lines (bounded by
    `qa_max_reads`, traversal-refusing, never executed). Empty model
    reply → ONE retry with a repair nudge, then honest error. Config:
    `qa_max_files` (4), `qa_context_lines` (80), `qa_max_reads` (6),
    `qa_max_read_chars` (4000).
  - **Task D — `harness.build_mode`**: `run_build(request_text,
    repo_path, config, log_root, task_id) -> {result, status,
    acceptance_tests, already_passing, note, ...}`. Stage 1: ONE model
    call authors acceptance tests (sanitize = fix-mode agent-tests);
    written into a PRIVATE base copy at `{task_id}.base` (a SIBLING of
    the task dir — NEVER inside `logs/{task_id}/`, which `_fresh_paths`
    archives on fresh start); baseline verify must show them FAILING on
    the pristine tree (passing = `already_exists`, reported honestly).
    Stage 2: the UNCHANGED fix loop with `config["target_test"]` = the
    acceptance tests and the additive `config["mode"]="build"` →
    state.json's `mode` key (six Boundary-4 keys stay a strict prefix).
    Config: `build_tests_max` (3), `build_tests_max_chars` (16000),
    `build_tests_dir` ("tests/_build_acceptance" — reserved,
    transient).
  - **Task E — `harness.research_mode`**: `run_research(question,
    config, log_root, repo_path=None, task_id) -> {answer, fetches,
    docs, status, ...}` — read-only by construction (no shell/sandbox;
    test-pinned). FETCH/DOCS control signals bounded by
    `research_max_fetches` (4), `research_max_docs` (4),
    `research_turns` (8); every fetch audited via `web_fetch` trace
    events; degenerate FETCH lines salvaged (an attempted fetch never
    silently degrades into an ungrounded answer); empty reply → ONE
    retry, then error.
  - **Task F — the live proof**: `logs/modes-round/four_modes_session.py`
    — one session, four real inputs + "hi", real cloud model + Docker:
    **19/19 checks** (fix verifier-gated on bug02_mean; question grounded
    in numlib/mathutil.py; build's mode() green through REAL Docker
    verify against its OWN authored tests; research answering the
    num2words `to="year"` question from 4 real web fetches; "hi"
    launching nothing). Five real defects found+fixed by that session:
    build's base-copy-inside-archive-dir bug; `library`-singular
    external-marker miss; spec-language "must raise ValueError"
    misrouting build→fix; empty-reply flakes minting success in
    qa/research/build-authoring; glued-URL FETCH parse crash
    (`parse_fetch` now single-token `[^\s<]+` + URL charset).
  - New trace events: `intent` {kind, reason, tier}, `intent_model`,
    `route` {kind, reason}, `qa_read`, `research_fetch_salvage`,
    `research_empty_reply_retry`, `qa_empty_reply_retry`,
    `build_tests_*`, `build_baseline_verify`, `build_mode_summary` —
    additive, safe to surface.
  - T4 (memory): decision-memory queries on read-only modes hit the
    same `open_default_store().search(repo_path=...)` surface; nothing
    new needed. T3 (runtime): no router changes — mode handlers set
    the same Boundary-2 context (the CLI session does this per call).
- 2026-09-13 (CLI session, two-tier config + custom router round):
  **`~/.vex/config.toml` is SUPERSEDED by the two-tier settings layout —
  the legacy file still works (fallback) but is no longer the target.
  No boundary signatures changed; Task.config passthrough keys
  unchanged.** New layout: global `%APPDATA%\vex\settings.toml` (Windows)
  / `~/.config/vex/settings.toml` (POSIX; `$VEX_CONFIG` wins) + project
  `<repo>/.vex/settings.toml` (committable) + `.vex/settings.local.toml`
  (personal; auto-added to the repo's `.gitignore` by Vex itself).
  Precedence: explicit flags/session > env (`VEX_MODEL`, `VEX_PROVIDER`,
  `VEX_BASE_URL`, `VEX_API_BASE`, `VEX_API_KEY`) > project-local >
  project > global > legacy > defaults — conflict-tested at every level
  (tests/test_cli_config.py, 42). `vex config path|list|get|set|unset|
  init-project` is the management surface (`set --tier global|project|
  local`; api_key masked in output; hand-written file comments survive
  appends; broken/BOM'd TOML is warned + skipped, never crashes the CLI).
  **Custom-router support (Task B):** `base_url` is a first-class
  settings key (aliased onto runtime's existing `api_base` context key
  by `cli.vexconfig.normalize_runtime_keys`, with `provider` defaulting
  to `"openai"` — any OpenAI-compatible endpoint + any model name, not
  a fixed provider list); wiring happens in `_make_task` (flag commands)
  and `_run_one_fix` (interactive). Proven live against the real
  TokenRouter endpoint with NO model flags (`base_url`+`model` from the
  settings file + `VEX_API_KEY` env → verified fix, logs/config-e2e/).
  T3 (runtime): no router changes needed — `api_base` context already
  existed; T1 (harness): nothing to ingest. Other terminals' docs/scripts
  that reference `~/.vex/config.toml` keep working via the fallback; new
  docs should cite `vex config path`.
- 2026-09-13 (Terminal 2 session, branding round): **CLI interactive
  intent gate (Task E — a REAL defect fixed: `hi` used to launch a fix
  task) + splash/compact-header branding + oxblood theme. Additive;
  no boundary signatures changed; state.json schema unchanged.**
  (a) **Intent gate** — new module `cli/intent.py`:
  `classify(line) -> Intent(kind: "fix"|"convo"|"ambiguous", reply)`
  (deterministic, offline, never raises). `run_interactive` now
  consults it BEFORE `_run_one_fix`: conversational input (greetings,
  meta questions about vex, thanks, chit-chat) is answered inline
  with NO task launched; ambiguous input gets ONE clarifying
  question; only bug-shaped input reaches the harness loop. Slash
  commands, bare session commands (repo/model/help/exit), and
  custom-command templates (cli/commands.py) are dispatched BEFORE
  the gate and never classified — they are explicit by construction.
  Regression-pinned in tests/test_cli_vex3.py (21 conversational +
  10 bug-sentence + 4 ambiguous cases + 2 real-loop wiring tests).
  (b) **Branding** — `cli/ui.py`: theme accent moved amber→oxblood
  ramp (owner-selected after a measured contrast check: true oxblood
  ≤2.1:1 on black, illegible; accent #C9504C ~4.7:1, running
  #D98E5F ~6.6:1; the referenced VEX_DESIGN_SYSTEM.md does not
  exist — third documented occurrence). NEW presentation helpers
  `ui.print_splash` / `ui.print_compact_header` / `ui.wordmark_lines`
  / `ui.TAGLINE`: blocky VEX wordmark (6×26, █ with # ASCII
  fallback), splash shown ONCE per log root (`interactive.
  _is_first_launch` — no session index + no traced run dirs;
  `_code-graph`-style artifact dirs don't count), one-line compact
  header (◆ ember mark + version + model + repo) every regular
  session start. (c) **Spinner hardening** — NEW `ui.SPINNER`
  (encoding-probed: braille "dots" only when the console can render
  it, else ASCII "line"): `spinner="dots"` was hardcoded in
  `ui.status()` and `LiveMonitor.start()` and was a latent
  cp1252/legacy-console UnicodeEncodeError (probe-reproduced, same
  class as the GLYPS fix). (d) **`_execute_task` gained an optional
  trailing `state=None` kwarg** (session context: quiet flag today) —
  DEFAULT-NOOP for all existing callers; the two vex2 test stubs
  were updated. LiveMonitor's stop() summary line reshaped to the
  status-line grammar (events | calls | tokens | cost). Verified:
  test_cli_vex3 49/49; full CLI sweep 113/113; adversarial 62/62;
  parallel-session suites (cli_config + cli_plugins) 71/71; live
  3-session drive 30/30 checks (spinner frames + live labels +
  cost ticker proven ACTIVE during a real Docker-sandboxed run).
  Report kept at Temp/opencode/vex-branding-check/.
- 2026-09-13 (Plugins & Skills round): **NEW Skills system (Task A) +
  custom commands (Task B) + plugin bundles (Task C). All additive; NO
  boundary signature changes; state.json schema unchanged.** (a)
  **Skills** — new module `harness/skills.py`: a skill is a folder with
  a `SKILL.md` (frontmatter: name + description-of-when-it-applies;
  body: the instructions), discovered at `<repo>/.vex/skills/` (project,
  shared) + `~/.config/vex/skills/` (global) + `~/.config/vex/plugins/
  */skills/` (from installed plugins) + config `skills_roots`. Before
  planning, the harness scans descriptions, reads the full SKILL.md of
  plausibly-applicable ones, and injects them as a new planner-prompt
  section `## Applicable skills` — placed AFTER `## Retrieved context`
  and BEFORE `## Constraints` (the same after-the-cut discipline as
  decision memory/coordination: T3's difficulty predictor cuts at
  `## Retrieved context`, and the placement is runtime-side
  regression-tested). Matching is conservative keyword/camelCase-word
  overlap over issue + retrieval terms + repo path segments, with
  task-domain stopwords (fix/bug/test/python...) so generic words never
  trigger a skill. New config keys (harness/config.py): `skills_enabled`
  (True), `skills_max` (3), `skills_max_chars` (2500), `skills_roots`
  (None). New trace event: `skills` {matched, considered, skipped,
  error, section_chars}. Best-effort: a broken scan degrades to
  "(none matched)" + trace error, never a planning crash. (b) **Custom
  commands** — new module `cli/commands.py`: `.vex/commands/<name>.md`
  (project) or `~/.config/vex/commands/<name>.md` (global) or
  `~/.config/vex/plugins/*/commands/<name>.md`; `/name args` in the
  interactive session fills `$ARGUMENTS` and runs the template as a fix
  request. Built-in slash commands can never be shadowed. (c) **Plugins**
  — new module `cli/plugins.py`: a bundle directory (explicit
  `plugin.json` manifest: name/description/version/skills/commands/
  tools.verbs/mcp_servers, or an IMPLICIT layout — everything under
  skills/ + commands/ with the dir as name). Installed under
  `~/.config/vex/plugins/<name>/`. CLI: `vex plugin install <local path
  or git URL>` (git = depth-1 clone to temp + local install),
  `vex plugin list`, `vex plugin remove <name>`; PluginError → clean
  message + exit 2. **Tool extensions**: a manifest's `tools.verbs`
  extend the BATCH read-only allowlist via new
  `harness.tools.extend_batch_verbs(verbs)` (merged inside the
  non-capturing group by `_batch_readonly_pattern`); a FIRST-TOKEN
  deny set (rm/sed/python/git/curl/...) makes hostile or malformed
  verb entries structurally unable to widen the guard, and the
  forbidden-composition guard still applies to extended entries.
  **MCP references** are recorded + surfaced (consumption goes through
  the EXISTING `vex mcp call/list-tools` — a plugin points at servers,
  it never becomes one). A registry/marketplace stays out of scope per
  the original stretch-list decision. (d) **evals**: task set is now
  14 (new scenario `eval_skills_injection` — a genuinely-matching
  pytest-conventions skill via config skills_roots; guards loop
  machinery, determinism preserved on both arms) and the arm set 8
  (new `no_skills` arm; `skills_enabled` joined `_ROUND_KEYS`, so
  pre_round turns skills off too). Prior reports (13×7) remain
  comparable per-task; only the new task/arm have no history. New
  tests: tests/test_skills.py (27, incl. three REAL-loop e2e:
  content-receipt of a matching skill's marker in the planner's own
  user message, irrelevant-skill non-injection, OFF-arm never scans) +
  tests/test_cli_plugins.py (30, incl. plugin install→skill-discovery→
  planner-injection roundtrip, hostile-verb deny list, CLI
  install/list/remove roundtrip, git-clone failure containment).
  Full eval matrix 14×8: 112/112 CLEAN, 0 regressions (the standard
  pre-ship gate for the planner-prompt change). Example skills shipped
  at tests/fixtures/skills/ (pytest-conventions, django-conventions,
  pandas-vectorization) and the example plugin demonstrating all three
  pieces (skill + /review command + ruff tool verbs + MCP reference) at
  tests/fixtures/plugin-webapp-toolkit/. T3 (runtime): predictor cut
  marker untouched (regression-tested via the skills placement test +
  `_issue_text_from` invisibility test); new `skills` trace events are
  additive/safe to surface. T4 (memory): nothing to ingest; the
  `skills` trace event is dashboard-surfaceable like decision_memory.
- 2026-09-13 (Terminal 1, web-page reading round): **NEW step-session
  control signal `FETCH <url>` — general-purpose web-page reading
  (generalizes the Round-8 DOCS docs-lookup scope). All additive; NO
  boundary signature or state.json schema changes.** New harness module
  `harness/webfetch.py` (Terminal-1-internal surface: parse_fetch /
  fetch_webpage / fetch_and_render / extract_readable_text). A step
  session may output `FETCH <http(s) url>` in place of a bash command;
  the harness (core.run_step, raw + fence-stripped parse — never
  executed as shell) GETs the page, extracts readable text
  (readability-style, stdlib-only: script/style/nav/header/footer/
  aside dropped, innermost semantic container preferred), and
  re-injects it into the live session. **Scope/safety (read-only by
  construction)**: GET only — no forms/auth/POST possible; scheme
  allowlist + SSRF host blocklist (loopback/private/link-local/
  reserved) with per-hop redirect re-validation (the fetch runs on the
  HOST, outside the sandbox, so the guard lives harness-side); timeout
  (15s), response cap (1 MiB, enforced DURING read), rendered-text cap
  (3000 chars). Every fetch trace-logs a `web_fetch` event
  {step_id, turn, url, ok, status, chars} (URL + timestamp + outcome —
  same auditability as any tool call) and emits to the unified stream
  (shared.tracing, module "harness"). New config keys
  (harness/config.py): web_fetch_enabled (True), max_fetches_per_step
  (3), webfetch_timeout_s (15), webfetch_max_bytes (1 MiB),
  webfetch_max_chars (3000), webfetch_max_redirects (3). New trace
  events: web_fetch (additive, safe to surface). Eval harness:
  `web_fetch_enabled` added to `_ROUND_KEYS` + new `no_webfetch` arm +
  new scenario task `eval_fetch_webpage` (task set is now 13; changing
  it invalidates prior eval comparisons — noted in evals/AGENTS.md).
  T3 (runtime): planner prompt + difficulty predictor markers are
  UNTOUCHED (the FETCH doc block is in the STEP system prompt only);
  state.json schema unchanged. T4 (memory): nothing to ingest — the
  `web_fetch` trace event is dashboard-surfaceable like tool_call.
  Proof: fixture bug07_num2words (num2words NOT installed host-side —
  DOCS/pydoc genuinely miss; the `to="year"` kwarg lives only on the
  library's web page) fixed through the REAL loop + REAL Docker verify
  with the real PyPI fetch mid-task; honest control: the plausible
  wrong kwarg (year=True → TypeError) genuinely fails an attempt.
  tests/test_webfetch.py (38), full eval matrix 13×7 arms 91/91 CLEAN.
- 2026-09-12 (CLI session, PyPI packaging round): **Distribution name is
  `vex-harness` — the installed COMMAND stays `vex` (and the legacy
  `harness` alias). No code/contract changes; pyproject metadata +
  README install docs only.** Verified against PyPI's JSON API before
  building: `vex`, `vex-cli` (an AI CLI that itself installs a `vex`
  command — direct conflict), `vexx`, and `pyvex` are all TAKEN;
  `vex-harness` (chosen), `vex-agent-cli`, `vexcli`, `vexfix`, `vexai`,
  and `vex-code` are available. `python -m build` output
  `dist/vex_harness-0.1.0-py3-none-any.whl` + `.tar.gz` (145-file
  sdist, source packages only — no logs/fixtures swept in); wheel
  METADATA now carries readme/authors/urls/classifiers/keywords.
  Verified by CLEAN-VENV install (fresh venv, `pip install
  dist/vex_harness-0.1.0-py3-none-any.whl`): all 8 top-level packages
  import, `vex --help` / `--version` (0.1.0+source) / fix / status /
  memory / dashboard / mcp all resolve, and the offline demo entry
  point runs without a key. Publishing (twine upload) deliberately
  NOT attempted — needs the owner's PyPI account + API token.
  Install command: `pip install vex-harness` → `vex`.
- 2026-09-12 (CLI session, interactive-mode verification): **One REAL
  bug found live + fixed in harness/editor.py::snapshot; no contract
  changes.** `cd <repo>; vex` (the no-args interactive flow) defaulted
  its log root to ./logs INSIDE the target repo; snapshotting the repo
  into logs/{task_id}/pristine then hit dst-inside-src and RecursionError
  (task "error" before the first model call). Every prior caller placed
  logs outside the repo, so the shape was untested. Fix: snapshot now
  excludes the dst parent-chain's top segment (e.g. `logs`) from the
  copy when dst lives under src — signature, Boundary 3/4 schemas, and
  all other callers unchanged (log-root-outside-repo behavior is
  byte-identical; a same-named `logs/` dir that is real repo content
  still copies). Regression-pinned both directions in
  tests/test_editor_prompts.py (4 new). The interactive flow itself
  re-verified live end-to-end through the real vex.exe (12/12 checks:
  banner, prompt, typed-sentence-as-issue, real loop, SUCCESS + diff +
  rationale, session index, never-mutate, rc 0) — details in
  cli/AGENTS.md "Interactive-mode verification round".
- 2026-09-12 (Terminal 1, Improvement Round 2 — agent-written tests):
  **`VerificationResult.structured_feedback` restored (additive, default
  `[]`).** The Round-8 delta was lost to the git-hook incident (the
  recovery note below delegated the re-write to T1); this round restored
  it exactly per the original design: an OPTIONAL `List[Dict]` of
  Boundary-7 FeedbackObject-shaped dicts (test_id, failure_type,
  summary, expected, actual, file, line, traceback_summary) filled by
  producers on failing runs; consumers treat absent/[] as "raw_output
  only" (graceful adoption — stub and historical serializations never
  carry it). `runtime/serialize.py` (T3's file, flagged here) round-
  trips the field with a default so older journals replay cleanly.
  Consumers: `_structured_or_tail` (harness/core.py) renders
  execution.feedback.format_objects when present, raw tail otherwise.
  ALSO this round (harness-internal, no boundary change): the
  agent-written edge-case test gate — after final verify passes, one
  model call writes edge-case tests (issue + diff in the prompt);
  sanitized filenames, compiled content, run through the SAME verify()
  in two stages (baseline rerun=0 on a pristine copy — a pre-passing
  generated test probes nothing and is dropped; post-fix with the final
  gate's flake-rerun + suite regression on a transient tree OUTSIDE
  work/). A post-fix failure poisons the attempt (the failing test is
  the next attempt's feedback); a GENERATION problem skips the gate
  (never overturn a verified fix over test-writing quality). Config:
  `agent_tests`, `agent_tests_max`, `agent_tests_dir`,
  `agent_tests_max_chars`. The self-critique gate (Agent Intelligence
  round) was also restored from the same incident (config keys
  `self_critique`, `self_critique_max_chars`; one review call of the
  diff vs the ORIGINAL issue; "no" poisons the attempt) — the sc8-*
  ablation logs referenced it; tests/test_self_critique.py passes again.
- 2026-09-11 (Observability & Eval round): **NEW cross-module tracing
  layer in shared/ — additive, no boundary changes.** `shared/tracing.py`
  (emit/emit_run/readers + safe_segment) gives every layer one
  normalized per-task event stream at `$VEX_TRACE_DIR/_trace/
  <task_id>.jsonl`; `shared/traceview.py` reconstructs a task's full
  lifecycle by merging that stream with the harness trace.jsonl, the
  worker journal, and the routing ledger. Contracts for emitters
  (never-raise, opt-in env, one-file-per-task, epoch ts) and the
  landed emitters per module are documented in shared/AGENTS.md.
  Consuming-module notes: execution/sandbox derives its task id from
  the mounted repo path (Win32 native backslash paths now trace — the
  original blanket rejection disabled execution tracing on Windows;
  tests/test_tracing.py pins both forms); runtime/model_router takes
  task_id from the existing router context; no emitter is required —
  absent VEX_TRACE_DIR the whole layer is a zero-overhead no-op, so
  existing tests/suites are unaffected. Also landed this round: the
  internal prompt-regression eval harness `python -m evals.run`
  (12 fixed tasks x 6 arms, real loop, exit 2 on regression; the
  standard pre-ship gate for prompt changes — evals/AGENTS.md) and a
  Win32 fix + eval-layout support in traceview's readers.
- 2026-09-10 (Terminal 3, Improvement Round 2): **Multi-candidate ensemble
  routing — additive mode, NO boundary changes.** New `runtime/ensemble.py`
  (task-level driver composing the existing scheduler/worker/harness
  machinery): predict task difficulty once from the issue text (same v2
  predictor + planner message shape the per-call router ingress scores —
  the hints are equal by construction and test-pinned), easy/medium →
  one sub-task with the ON arm's exact routing config, hard → TWO
  parallel cheap-pinned full candidate runs, escalate to ONE expensive
  run only if both miss. `runtime/model_router.py` and
  `runtime/difficulty.py` are byte-for-byte unchanged (single-attempt
  adaptive routing is NOT replaced — ensemble is a separately-ablated
  additional mode; three-arm comparison in runtime/AGENTS.md). The
  ablation runner grew `--arm ensemble` (+ a three-arm delta block +
  honesty notes in summary.json). New tests/test_ensemble.py (11,
  offline). Sub-task ids `ens-{a|c1|c2|x}-{slug}` share the run's
  logs root; per-bug target_test/test_command passthroughs ride the
  same keys the multirepo set uses. All surfaces runtime-internal.
- 2026-09-10 (Terminal 1 + Terminal 4, joint round): **Memory-informed
  planning — the planner now actively queries decision memory before
  planning, plus the ablation that measured it.** Contract changes:
  (a) **Boundary 4 ADDITIVE key**: state.json now also carries
  `repo_path` (written after the six schema keys, only when non-empty).
  Terminal 4's `ingest_state_file` has read `data.get("repo_path")` since
  Round 1 — the reader predated the writer; ingestion now stamps rows so
  repo-scoped queries work. Consumers must keep treating extra keys as
  ignorable (the six-key prefix order is unchanged and still asserted by
  tests). (b) **Terminal 4 surface extensions** (additive, defaults
  preserved): `DecisionStore.search(query, limit, repo_path=None)` —
  optional repo scoping, compared via normcase+resolve so relative and
  absolute forms of the same repo match; `memory.decision_store.
  open_default_store()` — the single opener all consumers should use
  (resolves memory.paths.decisions_db_path, i.e. HARNESS_DECISIONS_DB
  env-overridable — the ablation pins it per-run). (c) **New harness->
  memory consumption** (programmatic, per the Boundary 5 note above):
  before the planner call, `run_task` queries the store for decisions
  recorded against THIS repo and injects them as a new planner-prompt
  section `## Relevant past decisions`, placed AFTER `## Retrieved
  context` and BEFORE `## Constraints` — deliberately after Terminal 3's
  predictor cut marker so decision memory never shifts difficulty
  scoring (regression-tested from the runtime side of the contract in
  tests/test_decision_memory_planning.py). New harness config keys
  (defaults in harness/config.py): `plan_with_memory` (True; False = the
  ablation OFF arm, exactly one code path), `memory_query_limit` (6),
  `memory_max_chars` (1500). New trace event: `decision_memory`
  {query, matched, error, section_chars} (or {skipped} when off). All
  best-effort: missing module/broken store degrades to "(none recorded
  yet)" + error in the trace, never a crash. New module
  harness/decision_memory.py; tests/tests file:
  tests/test_decision_memory_planning.py (17).
  **Ablation (Task B, honest result)**: 5 fixture tasks where relevant
  prior decisions were seeded (genuinely mined from the v4/v3 ablation
  traces — the bare-pytest ImportError class that burned real turns
  before, sed-quote breakage, per-repo suite-invocation conventions —
  recorded via DecisionStore.record with repo_path, the documented
  path for facts not in state files; dedicated DB, production store
  untouched). Both arms real stack, same pinned model (z-ai/glm-5.3-free,
  adaptive routing OFF — no routing confound), arms differ only in
  plan_with_memory. Result: 100% success, 1 attempt, 0 verify failures
  BOTH arms — memory did NOT change task-solving power on this set. It
  changed efficiency: harness-level model calls 35->27 (-23%), tokens
  147,916->112,390 (-24%), proxy cost $0.2148->$0.1782 (-17%), and
  past-mistake recurrences 3->0 (three OFF tasks re-tripped the
  documented bare-pytest ImportError class; zero ON tasks did, and
  ON-arm commands visibly mirror the seeded conventions — python
  rewrite over chained sed, python -m pytest from repo root). Honest
  caveats: n=5x1rep directional only; each arm absorbed one symmetric
  mid-run crash-retry; the ledger's raw call counts include router-
  internal empty-response retries (the corrected trace-level counts
  are what's quoted); a parallel terminal's in-flight agent-tests
  feature ran symmetrically in both arms. Driver:
  runtime/ablation_memplan.py; full data + post-run analysis:
  logs/memplan-ablations/run1/summary.json.
- 2026-09-10 (Terminal 1, Round 8): **Agent execution layer & tooling —
  structured tool errors, BATCH, lint gate, DOCS lookup. All additive;
  no boundary signature or state.json schema changes.** New harness
  module surfaces (Terminal-1-internal, no cross-module contract
  changes): harness/tool_errors.py, harness/lint.py,
  harness/docs_lookup.py. (a) **Structured tool errors (Task A)**:
  every failing tool result is classified into a stable error kind —
  model-facing `TOOL ERROR [<kind>]: <detail>` + suggested fix, 10
  classes (file_not_found, command_not_found, malformed_patch,
  syntax_error, import_error, undefined_name, permission_denied,
  timeout, argument_error, command_rejected) + internal_error fallback;
  classify() never raises; exit=0 output format UNCHANGED.
  (b) **BATCH protocol (Task B)**: a step session may issue
  `BATCH <read-only cmd> ;;; <cmd>` — strict verb allowlist + forbidden
  composition chars, all-or-nothing validation, executed concurrently
  (ThreadPool 4) via throwaway sessions; measured 1.97× on an 8-op read
  phase (real Docker). Deliberately NOT generalized: read-only,
  order-independent only. (c) **Lint gate (Task C)**: stdlib AST pass
  (syntax + module-level undefined names, false-negative biased) at
  TWO points — SUBMIT-time in-session feedback, and a pre-final-verify
  short-circuit that retries WITHOUT burning a verify cycle on an edit
  the AST pass knows is broken. Never gates success (verifier-gated
  completion unchanged). New config keys: lint_gate (True), lint_names
  (True). (d) **DOCS protocol (Task D)**: `DOCS <target>` resolves
  library/API docs cache → pydoc (subprocess) → opt-in PyPI (gated OFF
  by default); read-only; shared cache at logs/_docs-cache/ (harness-
  owned, outside any repo — same convention as _code-graph/; harmless
  to prune). New config keys: docs_lookup_enabled (True),
  docs_lookup_allow_remote (False), max_docs_per_step (3),
  docs_max_chars (3000). New additive trace events (safe to surface):
  batch_call, batch_rejected, tool_error, lint_failed, docs_lookup.
  New tests: tests/test_tool_errors.py (26) + tests/test_batch_docs_
  lint.py (28), both Docker-free; full module selection 204 green.
  NOTE for all terminals: a parallel `git reset --hard` at ~09:37
  destroyed uncommitted harness work tree-wide (Round-6 hardening was
  recovered from dangling stash fa75dcb; my Round-8 wiring re-applied;
  the state-machine/self-critique session re-landed their own) — commit
  or back up outside the tree after every green milestone.
- 2026-09-10 (Terminal 1, Improvement Round 2): **Coordinated
  multi-file changes — detection + atomic group validation/rollback.**
  (a) **Boundary 4 ADDITIVE key `change_groups`** (schema updated
  above): planner steps may now carry `"change_group": "<name>"`; the
  harness unions each group's files_hint into
  `state.json["change_groups"] = {name: [files]}` — written after
  repo_path, absent when the plan declares none, hydrated on resume,
  malformed-value-tolerant. Consumers MUST treat it as ignorable per
  the additive-key rule (same standing as repo_path). (b) **New module
  harness/coordination.py** (Terminal-1-internal; consumes
  memory.code_graph's raw Graph like retrieval does — no new
  memory-side surface): `detect_coordinated_change(repo_path,
  issue_text, changed_files, plan_texts?, index_root?, kind?,
  max_files?, protected_patterns?) -> dict` — structural fan-out via
  CALL edges (callers of symbols defined in changed files) + IMPORT
  edges (importers of changed modules), classified by issue vocabulary
  ("signature" / "rename"); never raises, degrades to
  detected=False without the graph. Protected paths (tests/*, VCS)
  are EXCLUDED from suggested groups (`excluded` key) — an atomic
  agent-edit group can never include a forbidden path. (c) **Plan
  schema extension (planner-facing, additive)**: the optional
  change_group field per step; the planner prompt gains a
  `## Coordinated-change fan-out` section (AFTER `## Retrieved
  context` — T3's difficulty predictor cut marker is unchanged) and a
  change_group schema line in PLANNER_SYSTEM. (d) **Atomicity
  semantics in run_task**: only plan-DECLARED groups are enforced (the
  detection is advisory — it shapes the prompt; the declaration is
  the commitment). Pre-final-verify gate: a group with some-but-not-
  all members changed poisons the attempt (feedback names the missing
  files) and rolls the GROUP back together via new
  `editor.restore_group` (config `coordination_rollback`:
  "group" default | "all" | "none"); a FAILED verified attempt also
  rolls touched groups back together. New trace events:
  coordination, change_groups, coordination_gate_rejected,
  coordination_rollback — safe to surface in dashboards. New config
  keys (harness/config.py): coordination_detect, coordination_gate,
  coordination_min_files, coordination_rollback. Tests:
  tests/test_coordination.py (31 unit) +
  tests/test_coordination_e2e.py (5 e2e, Docker-gated — a GENUINE
  4-file scenario, fixture tests/fixtures/bug06_coord: method rename +
  flag removal rippling model -> serializers -> reports -> api), both
  added to harness-ci.yml. Full 265-test harness selection green.
- 2026-09-10 (DX round, root tooling): **Dev-experience tooling landed —
  NO boundary signature changes anywhere.** (A) `scripts/dev-setup.sh`
  (+ `make dev`): one-command setup — deps (`pip install -e ".[dev]"`,
  auto-venv on PEP-668 hosts), pre-commit hook install (preserves a
  user hook as `.user-*`), Docker verification, CLI smoke. Idempotent.
  (B) **The repo now has ONE canonical linter: ruff** (`[tool.ruff]` in
  pyproject.toml). The pre-commit hook (`scripts/hooks/pre-commit`),
  `make lint`, and CI all use it. RULE FOR ALL TERMINALS: any future
  lint surface — including an in-sandbox agent lint tool — must REUSE
  this config, never add a second linter. Pre-linter debt is capped per
  file by a RATCHET (`scripts/lint_ratchet.py` +
  `scripts/lint-baseline.txt`): new files must be violation-free;
  baselined files may not gain debt; `--update-baseline` is a
  deliberate action. (C) `cli/errors.py`: every CLI failure surface
  (fix/run-benchmark/interactive/resume/top-level net/`python -m cli`
  import guard) now prints a plain-language cause + "check:" lines;
  raw tracebacks are saved to a file, never displayed. Exit-code
  contract (0/1/2 + 130) unchanged. (D) **Operational incident +
  recovery, all terminals read this**: a `git reset --hard` during
  hook testing destroyed the tree's uncommitted Round-8 tracked-file
  deltas. The vex-era working tree was recovered from
  stash tag `recovery-stash-round7-sim2` (20 files, verified
  identical to the pre-incident disk state); T1's 03:23 state-machine
  core/config/prompts snapshot is preserved under tag
  `recovery-stash-t1-agent-intel` (their in-flight core.py re-writes
  were observed landing after, so the tag is a fallback, not a
  restore source); harness/retrieval.py (size_context_budget /
  rerank_files), shared/types.py (structured_feedback) and
  execution/verify.py (Boundary-7 feedback fill) Round-8 deltas were
  NOT recoverable from git — T1/T2 please re-write those from your
  session context. Untracked Round-8 files all survived untouched.
- 2026-09-09 (Terminal 1, Round 6): **Opt-in model completion-token budget
  (ctx key "max_completion_tokens") + adversarial-hardening notes.** Two
  runtime-owned changes landed during the Round-6 multi-repo validation, both
  ADDITIVE and opt-in (absent key = previous behavior; no boundary signature
  changed anywhere): (1) `runtime/worker.py` passes
  `cfg.get("max_completion_tokens")` into `set_call_context`, and
  `runtime/model_router.call_model` forwards it as litellm `max_tokens`
  when set. Motivation: the tokenrouter endpoint began burning an unbounded
  token budget on hidden reasoning_content (content=None +
  finish_reason=length, twice on the R6 planner prompts at 4000-token
  budgets, 1453s/265s); an explicit budget guarantees room for the visible
  answer. (2) T3's test-encoded intent that AuthenticationError is NOT
  retried was deliberately PRESERVED (T1 probed adding a retry, found the
  module's own test forbidding it, and reverted — the observed gateway auth
  flake remains a documented endpoint-health note, not a code change).
  Separately, harness-owned adversarial hardening (tests/test_adversarial.py,
  43 tests): `editor.is_protected` now '..'-normalizes paths and treats
  .git/.hg/.svn as ALWAYS protected; `editor.check_edits` scans work/ for
  agent-forged VCS paths (changed_files deliberately skips them, so a
  diff-only check never saw the class); `tools._DENY_PAT` covers reordered
  rm flags ('rm -fr /') and pipes into bash/zsh/dash from curl AND wget.
  CRITICAL harness fix found by the e2e prompt-injection test:
  `core.run_task` now re-runs `check_edits` before final verify — a
  protected-path violation used to poison only the STEP, and a defused test
  + real fix could still mint a verifier-gated SUCCESS (real success-path
  bypass, reproduced, fixed, regression-tested).
- 2026-09-09 (Terminal 2, Vex CLI Side Task 2): **CLI session persistence
  + slash commands + config file + plan preview.** Four additions, all
  leaning on existing backend contracts (no boundary changes; exit-code
  contract 0/1/2+130 intact). (A) `vex --continue` /
  `--resume <task_id>` / `--list-sessions`: a session index
  (logs/.vex-sessions.jsonl, recorded on completion AND interrupt) plus a
  directory-scan fallback discovers resumable runs (resumable = state.json
  completed AND remaining steps AND no final result event — exactly the
  harness resume contract's precondition; pre-step-1 interrupts correctly
  NOT resumable, by the documented fresh-restart semantics). `_resume_task`
  rebuilds the Task from the run's own task_start trace event (the public
  observability surface) with config["resume"]=True — LIVE-verified:
  child hard-killed after step 1 → dir-scan flags resumable → resume
  skips the completed step, continues the in-flight attempt, finishes,
  git output + rationale produced. (B) Interactive slash commands
  /status /diff /sessions /resume /approve /reject /cancel /quiet /help —
  /approve//reject write decision.json via runtime.approval's EXISTING
  file protocol; /cancel is SIGINT semantics (checkpoints kept). (C) NEW
  cli/vexconfig.py — ~/.vex/config.toml (or $VEX_CONFIG): model, provider,
  budget_cap_usd, max_retries, plan_preview, log_verbosity, log_root;
  precedence explicit-flags > file > DEFAULTS; broken TOML warns once and
  is ignored, unknown keys pass through. (D) plan_preview (config key,
  default OFF = autonomous): renders the FIRST plan trace event (numbered
  steps + checkpoints) and prompts before the edit phase; reject cancels
  with checkpoints kept; reuses the Ctrl+C machinery, NOT a new protocol —
  the worker-level final-diff approval gate is unchanged and separate.
  NEW tests/test_cli_vex2.py (24); full CLI regression 126/126.
- 2026-09-09 (Terminal 2, Vex CLI side-task): **PROJECT RENAMED Vex —
  Boundary 6 entry point is now `vex`; interactive natural-language mode
  is the primary UX.** Coordinated with Terminal 4's Round-6 state (their
  adversarial hardening is complete and untouched: 62/62 green; only 3
  decorative output assertions in tests/test_cli.py updated to the new
  rich-render format, same intent — flagged for T4's review). Changes:
  (a) pyproject name=vex, console script `vex = "cli.main:main"`,
  `harness` kept as a working alias until docs migrate (demo/ still says
  `harness fix` — T4's module, deliberately untouched); argparse
  prog="vex". (b) NEW cli/ui.py — shared rich Console + VEX_THEME
  (amber/ember palette) applied to every command; ui.GLYPHS with
  encoding-probed ASCII fallbacks. (c) NEW cli/interactive.py — no-args
  `vex` on a TTY = plain-language session (sentence becomes the issue
  text, repo from CWD, repo/model switching, status/diff re-render);
  non-TTY no-args = usage error, never a hang. LiveMonitor tails
  trace.jsonl (the public observability surface — no run_task internals
  reached) for a live spinner + accruing cost during runs; benchmark live
  table; approval prompts render the request.json diff and write
  decision.json; Ctrl+C sweeps this process's orphaned containers via
  execution.sandbox's public reaper. (d) **Two real cross-platform bugs
  found + fixed by verification, not assumption**: `pip install -e .` was
  broken for everyone (flat-layout package discovery — explicit package
  list added; `vex.exe`/`harness.exe` now install and run on Windows,
  same flow Linux/macOS), and legacy cp1252 consoles crashed on the first
  glyph (UnicodeEncodeError in rich's LegacyWindowsTerm — GLYPHS
  fallbacks). (e) NEW tests/test_cli_vex.py (10: theme roles, no-color
  strip, diff classification, monitor tracking/rotation-survival,
  non-TTY dispatch subprocess, scripted interactive fix, interrupt sweep
  up/down). 109 CLI-adjacent tests green; rich>=13.0 added to
  dependencies. Exit-code contract 0/1/2 + 130 (interrupt) unchanged;
  no other contract surface changed.
- 2026-09-09 (Terminal 4, Round 6): **ADVERSARIAL HARDENING — one REAL
  data leak found + fixed (task-id path traversal), plus two CLI input-
  validation crashes; NEW shared surface in memory/paths.py.** No
  signature changes anywhere.
  (a) **The leak (live-confirmed before fixing)**: `mcp_server.
  task_status(task_id)` and `harness status --task-id <id>` built
  `logs/<task_id>/state.json` by naive path join — on Windows,
  `Path('logs') / 'C:/evil'` DISCARDS the base (drive-lettered
  operands) and `..`-chains resolve through, so a hostile task id
  read ANY state.json-shaped file on the host (reproduced against
  real production task data). Fix: both surfaces now resolve ids
  ONLY through the new shared guard `memory.paths.safe_task_dir/
  is_safe_task_id` — semantic rejection (separators, null bytes,
  `:`-drive forms, Win32 `' ..'`-style whitespace/dot tricks) +
  resolve-containment under the logs root; interior spaces/unicode
  stay allowed. If YOUR surface joins a task id onto a path, use
  this guard — do not re-derive.
  (b) CLI input validation: malformed subset entries (`{"repo": 123}`
  and `"config": "string"`) crashed with tracebacks; a null-byte
  `repo` spawned doomed scheduler workers (found live). All rejected
  at `_load_subset` validation time now; `--issue @file` handles
  binary/null-byte paths; exit-code contract (0/1/2) unchanged.
  (c) Verified-contained surfaces (no changes needed): query_
  decisions SQL injection (parameterized + literal-keyword ranking),
  secret harvesting (production DB swept: 0 secret-pattern hits),
  CodeGraph hostile queries (indexed-path misses only; content never
  served), MCP SDK exception containment (generic `Error executing
  tool` — no traceback leaks), CLI shell-injection payloads
  (`--repo`/`--issue` are data; zero shell=True/os.system/eval in
  the repo; marker-file side effects checked: none).
  (d) Tests: tests/test_mcp_adversarial.py (39) +
  tests/test_cli_adversarial.py (62), incl. the exploit replayed over
  the REAL stdio transport and a real `python -m cli` subprocess.
  Production audit artifacts: logs/mcp-adversarial/ (19/19) +
  logs/cli-adversarial/ (38/38). Full per-probe outcome tables:
  mcp_server/AGENTS.md, cli/AGENTS.md, memory/AGENTS.md (Round 6).
- 2026-09-09 (Terminal 2, Round 6): **ADVERSARIAL SECURITY TESTING —
  sandbox CONFIRMED under deliberate attack; no contract changes, no
  production-code changes.** New `execution/sandbox_adversarial.py`
  (NOT in pytest — run explicitly, like sandbox_stress.py): Task A
  sequential (`python -m execution.sandbox_adversarial`) + Task B
  concurrent (`--concurrency N`). 24/24 sequential attacks HELD (escape:
  host mounts/FS/shadow/pidns/docker-socket/su/chown/proc-mount/
  workspace-siblings/network/env — all blocked; resources: fork bomb
  collapsed at pids-limit, 2GB mem bomb+leak OOM-killed (137), tmpfs dd
  ENOSPC'd at exactly the 256m cap, CPU ratio 3.7-6.2x under --cpus 1.0,
  infinite spin timeout-killed, 100MB output flood returned bounded).
  Cross-container: filesystem/network/bridge-scan isolation all held;
  victims undisturbed. Task B: 78 concurrent hostile runs at widths
  8/10/16 (16 simultaneous bombs on the 12-core VM) — 0 findings,
  canary tasks clean. ONE DESIGN FINDING recorded (not a bug): the RW
  bind mount lets a container write host disk unquota'd (no docker
  primitive for bind-mount quotas) — inherent to the RW-mount contract
  (T1 diffs host-side); see execution/AGENTS.md Round 6 for the full
  attack/evidence table. 9 permanent regressions added in
  tests/test_sandbox.py::TestSandboxAdversarial (module suite 84/84).
- 2026-09-09 (Terminal 2): **FLAKE FIXED — `test_concurrent_burst_no_leak`
  (the flag filed by Terminal 3, below — CLOSED).** T3's diagnosis was
  confirmed empirically before fixing (20 consecutive `docker images`
  capture pairs over the unchanged 25-image cache: 12/20 raw-list
  mismatches, 0/20 sorted; the cache holds an exact same-second pair from
  the Round-4 builds), and a live reproduced red exposed a SECOND,
  coexisting mechanism in the same test: the one-shot `docker ps -a`
  residue assertion catching a container still in daemon-async `--rm`
  teardown right after the 20-burst (the mechanism Terminal 2's own
  Round-4 note suspected). Fix is tests-only (no production code, NO
  contract change — the production module never had the ordering bug):
  (a) image captures now go through `_image_set()` (sorted/set
  comparison, T3's suggested fix) with growth bounded to the repo's own
  dep tag (`grew <= {tag}`, the sandbox_stress.py pattern); (b) all three
  one-shot residue assertions in tests/test_sandbox.py (this test,
  `test_no_container_left_behind`, `test_orphaned_container_reaped_by_
  peer`) now poll via `_assert_no_hexec_residue(timeout_s=15)`, scoped
  to THIS process's `hexec-p<pid>-*` containers — the scoping matters on
  this machine: parallel pytest suites share one Docker daemon, and a
  global `hexec-` check sees the other suite's LIVE containers (found
  live when two verification suites ran concurrently). Teardown lag
  can't false-red, real leaks still fail loudly, and the orphan-reap
  test now asserts the victim-gone invariant (a concurrent peer's
  opportunistic sweep reaping the victim first is the self-healing
  mechanism WORKING, not a failure). Two regression tests pin the fix
  under the trigger conditions themselves:
  `test_regression_image_list_ordering_immunity` (8 back-to-back
  captures, every pair must match — the ordering hazard, formerly 12/20
  raw mismatch rate) and `test_regression_burst_back_to_back_with_
  other_tests` (predecessor run + immediate 20-burst, the exact
  order-dependent regime that historically went red). Verified: module
  suite 75/75 in order; 4 consecutive trigger-order integration-class
  runs green; 2 randomized-order full-suite runs green (pytest-randomly,
  distinct seeds); two integration suites run CONCURRENTLY against the
  shared daemon both green — the regime that false-reded the first
  iteration of this fix. Real Docker throughout. No other module needs
  to act — this was test mechanics, not behavior.
- 2026-09-09 (Terminal 1): **Round-5 closeout — spec item 13 (reversible
  compaction) finished for real + T3's state.json flag fixed.**
  (a) **RECALL protocol (on-demand reinjection, item 13)**: a step session
  may output `RECALL <terms>` in place of a bash command. The harness
  (core.run_step) intercepts it — on both the raw reply and its
  fence-stripped form, so a fenced RECALL is never executed as shell —
  greps THIS task's trace.jsonl via the new `TraceLogger.find_events(query,
  kinds=None, limit=5, max_chars=4000)` (case-insensitive substring over
  kind+data, most-recent-N, per-entry char cap, malformed lines skipped,
  never raises), and re-injects the matching entries into the live
  session's context as a user message ("RECALL results for '<query>'").
  state.json remains the compacted view; trace.jsonl the full-fidelity
  store; RECALL is the retrieval hook between them — exactly what item 13
  meant by "reversible." Budgets (all task.config, defaults in
  harness/config.py): `max_recalls_per_step` 3, `recall_results_cap` 5,
  `recall_max_chars` 4000; budget exhaustion nudges back to bash, never
  deadlocks. New trace event: `recall` {step_id, turn, query, matched}.
  Consumers: T4's dashboard may surface `recall` events (a step that
  pulls old detail back is interesting signal); T3's difficulty
  estimator keys on PLANNER prompt markers (`## Issue` / `## Retrieved
  context`) — those are UNCHANGED; the RECALL doc block was added to the
  STEP system prompt only, so your predictor's markers are intact.
  (b) **T3's 2026-09-09 flag (cli-real-smoke state.json: success with
  completed_steps: []) — CLOSED.** Root cause (from that run's trace):
  the step's commands applied the fix but the turn budget exhausted
  before SUBMIT (ok=False "exhausted ... turns"), final verify then
  passed and the task succeeded — with complete_step never called. Fix:
  on a VERIFIED success, the harness now records every plan step that
  RAN in the winning attempt as completed (new
  `TaskState.complete_all_ran_steps(steps)`; the verified diff subsumes
  each ran step's work). state.json schema unchanged; `harness status`
  now renders true progress on that path. Regression-tested
  (exhausted-turns success → completed_steps == plan, remaining empty).
  (c) Test status: 107/107 harness tests green (78 prior + 26 RECALL
  unit + 3 new e2e: cross-session RECALL reinjection proven
  content-receipt-wise, RECALL budget-exhaustion, exhausted-turns
  success-state).
- 2026-09-09 (Terminal 2 flag, filed by Terminal 3):
  `tests/test_sandbox.py::TestSandboxIntegration::
  test_concurrent_burst_no_leak` went intermittently red after Round-4's
  real-model runs (ablation v4 + CLI real smoke built ~13 new dep images
  in rapid succession). NOT a container leak — the failing assertion is
  the before/after `docker images` LIST comparison: several images now
  share a creation second, and `docker images` orders same-second images
  nondeterministically, so two consecutive invocations can differ at
  those indices. Suggested fix (your module, your call): compare SETS
  (sorted) instead of lists, or filter to the test repo's own tag.
  Verified green before tonight's runs; ~1-in-2 flaky now, purely from
  image-list ordering.
- 2026-09-09 (Terminal 3): **Round-4 continuation — Task B/C closure.**
  (a) **Ablation v4 re-run** (`logs/ablations/v4/`, both arms in one
  invocation, 16/16 tasks green in BOTH arms — fixture-path fix
  confirmed): OFF 100%/$0.1505 vs ON 100%/$0.0581 (2.59× cheaper,
  3.3× faster wall; 68 cheap + 3 expensive calls; 1 genuine
  struggle escalation, 1-of-2 scary-text false escalation). Together
  with `v3-expanded-fixed` (3.47×) this makes the Phase-5 result
  REPRODUCED twice in independent runs. (b) **Ablation summary-merge
  fix**: separate `--arm` invocations sharing one `--out` now MERGE
  into summary.json (arms accumulate + `arm_runs` provenance) instead
  of clobbering — the Round-3 gotcha is fixed; running both arms in
  one invocation remains the default. (c) **FAKE-path split-brain
  FIXED (runtime-owned)**: the fake harness's state.json default moved
  from repo-CWD `./logs/{task_id}` to the PINNED log_root (both
  writer and reader now resolve through `runtime.paths.
  state_json_path`) — `harness run-benchmark --log-root <custom>` +
  fake harness previously broke resume outside the repo root.
  `fake_state_dir` config still overrides (all existing tests
  unchanged). Verified through the CLI: 3-task fake subset @ custom
  root with a mid-run kill → resume=True, all artifacts under the
  custom root, zero leakage into ./logs. (d) **REAL-harness path
  through the CLI verified**: one smoke_repo task, real model,
  adaptive routing ON, `hang_heartbeat_stale_s: 300` → success,
  $0.0081, all artifacts under the custom root. Honest note: the
  first worker was hang-killed at exactly 300s (state_stale) because
  the cheap tier's first planner call ran >300s under load, then the
  requeued relaunch finished — T4's 300s guidance is BORDERLINE when
  the cheap tier is loaded; 600+ is safer (the ablation uses 1200).
  (e) **New config key (test/offline only): `mock_script`** —
  plan+per-step bash scripts from a plain dict, consumed by
  runtime.worker via `runtime.mock_provider.install_script` so the
  REAL harness loop runs deterministically in subprocess workers
  with zero network (used by `runtime.stress --mode real`).
- 2026-09-09 (Terminal 4): **MCP client + `harness mcp` CLI surface**
  (spec item 30, "consume external MCP tools"): new
  `memory/mcp_client.py` — minimal stdio MCP CLIENT using the same
  official SDK (`list_mcp_tools(server, cwd?, env?)`,
  `call_mcp_tool(server, tool, args?, cwd?, env?)`; results are
  plain dicts {"ok", "text"/"tools", "error"}, never raises). CLI:
  `harness mcp list-tools "<server cmd>"` and `harness mcp call
  "<server cmd>" <tool> [--args '{...}']` — the demoable
  "consume an external MCP server" path; our own mcp_server doubles
  as the test target (12 tests, tests/test_mcp_client.py: real
  subprocess round-trips incl. env passthrough, quoted-Windows-path
  argv handling, bad-server/bad-tool → ok=False, never a traceback).
- 2026-09-09 (Terminal 1 flag, filed by Terminal 3): in the Round-4
  CLI real-run (`logs/cli-real-smoke/`), the final state.json of the
  successful task shows `completed_steps: []` + `remaining_plan:
  [step]` while trace/state agree the task finished (result:
  success, decision recorded). The task was hang-killed pre-step and
  relaunched fresh, so this looks like a resume/restart path in
  harness.core not re-marking the step complete on the final write.
  Cosmetic (progress authority disagrees with result), but
  `harness status` renders "0/1 complete" for a successful task.
  Terminal 1: please check the final state write on the
  killed-then-restarted path.
- 2026-09-09 (Terminal 1): **Round-4 report — first full-stack DoD on an
  unfamiliar real OSS repo (jaraco/path) + a harness bug found live and
  fixed.** (a) **Self-audit of spec items 1-17: COMPLETE, all CORE items
  implemented** — full per-item verdicts in harness/AGENTS.md Round 4.
  Honest weak spots documented (item 13 has no on-demand reinjection of
  older detail — trace.jsonl keeps everything, state.json is the compacted
  view; failure classification remains Phase-3+ work at the documented
  `last_feedback` seam). (b) **harness/editor.py behavior change
  (internal semantics, no signature changes)**: `changed_files()` now
  EXCLUDES verifier-created run artifacts (`.coverage`/`.coverage.*`,
  `.hypothesis`, `.cache` dirs, plus the pre-existing skip set) and
  `unified_diff()` treats a non-UTF-8/binary changed file as binary
  (returns None) instead of raising UnicodeDecodeError. Why: a real OSS
  run crashed AFTER final verify passed because the repo's pytest
  materialized a binary SQLite `.coverage` in work/ — the strict decode
  was outside its guard. A verified fix must never die over presentation.
  Consumers: T4's dashboards / status readers see cleaner files_touched
  (no `.coverage` entries); T2's git_output receives no artifact files.
  (c) **OSS-run proof (first non-fixture end-to-end)**: real Scheduler →
  worker subprocess → real harness → real cloud model (6 calls, $0.053)
  → real Docker sandbox → verifier-gated success in 1 attempt, WITH
  approval gate (request/decision file protocol), git branch+commit+PR
  description, rationale.md, and T4 memory ingestion confirmed via the
  MCP query_decisions surface. First attempt failed honestly (exposed
  (b)); artifacts under logs/oss-round4/.
- 2026-09-08 (Terminal 3): **Round-4 report.** (a) **T1's approval/hang
  finding FIXED** (their entry below): the runtime checkpoint
  (`logs/{task_id}.runtime/checkpoint.json`) now carries an
  `awaiting_approval` boolean — the worker sets it before parking in the
  approval gate and clears it ATOMICALLY with `status: "finished"` in
  the final checkpoint write (no separate finally-clear: that reopened a
  kill-during-teardown race, found live). The scheduler's state-stale
  hang kill SKIPS workers whose checkpoint has `awaiting_approval: true`
  (or `status: "finished"`) while their heartbeat is fresh; a dead
  heartbeat and the wall-clock cap still kill. Net effect: T1's "pin
  hang_heartbeat_stale_s >= approval_timeout_s" mitigation is no longer
  required for gate-parked tasks. Verified: unit tests (2 new live
  scheduler tests) + 45 overlapping real-harness parks, each ~30s past
  the stale window, 0 gate-parked kills. (b) **Stress re-verified at
  scale against the fuller harness** (T1's git output + rationale in the
  loop): real 45@cap45 w/ 8 kills — 45/45 tasks produced git.json +
  rationale.md + trace events while being killed/resumed; new
  stress-mode `--approval` reproduces the gate scenario at scale.
  (c) **Ablation final data** (Phase 5): 16-task paired run,
  `logs/ablations/v3-expanded-fixed` — OFF 100%/$0.185 vs ON 94%/$0.053
  (3.47x cheaper; 57 cheap + 2 expensive calls; single failure was a
  gateway flake, not routing). The earlier `v3-expanded` runs are
  INVALID (runner passed bare fixture dir names — 5 tasks errored at
  snapshot in both arms); fixed + re-run. Full write-up in
  runtime/AGENTS.md Round 4. (d) Measured for everyone's benefit: healthy
  real-harness work phases run up to ~98s between state.json writes
  under 45-way Docker load (p95 75s) — size hang_heartbeat_stale_s
  above REAL WORK gaps, not just model-call latency.
- 2026-09-08 (Terminal 1): **Round-3 report.** (a) **context.py repair
  (Terminal 4's 2026-09-08 entry) reviewed — CLOSED**: the deletion was
  correct; it matched my intended shape (ONE `_write`; `save_plan_steps`
  writes only plan.json). Regression tests added in
  tests/test_config_trace_state.py (import/AST parse of every harness
  module + single-`_write` structural invariant); verified they catch the
  original corruption by re-introducing it. (b) **New harness outputs on
  verified success (spec items 26/29), no contract changes to TaskResult**:
  `run_task` now writes `logs/{task_id}/rationale.md` (via
  execution.rationale) and `logs/{task_id}/git.json` (via
  execution.git_output — real branch + commit in the harness's PRIVATE
  work/ copy; original repo untouched) + `rationale`/`git_output` trace
  events. Both best-effort: failures degrade to trace events, never
  change the verifier-gated outcome. New task.config keys (harness-owned,
  documented in harness/config.py): `git_output` (bool, default True),
  `rationale_log` (bool, default True), `branch_name` (str, optional).
  Terminal 4: `harness status`/dashboard may surface rationale.md and
  git.json — they're plain files under logs/{task_id}/. (c) **New
  test-only cross-process hook**: env var `HARNESS_SCRIPTED_MODEL=<json
  spec>` makes harness.deps resolve a deterministic scripted model
  (harness/_stubs/scripted_model.py) inside SUBPROCESS workers where
  set_call_model injection can't reach. Never use in production. (d)
  **Approval-mode wiring CONFIRMED LIVE** end-to-end (scheduler → worker
  subprocess → real harness → request.json/decision.json with an external
  approver; both approve and reject paths) — see harness/AGENTS.md Round
  3. **Finding for Terminal 3**: a worker blocked in the approval gate
  stops touching state.json, so the scheduler's hang check (default
  `hang_heartbeat_stale_s: 30`) kills it mid-gate unless configs pin
  `hang_heartbeat_stale_s` >= `approval_timeout_s` (same mitigation as
  your long-model-call note). Suggested runtime-side fix: keep the
  heartbeat daemon beating while parked in the gate.
- 2026-09-08 (Terminal 2): Task-A integration spec for T1's run_task
  wiring of git_output/rationale is written out IN FULL in
  execution/AGENTS.md ("Task A (Round 3)" section): exact signatures,
  call order (build_rationale FIRST, then produce_git_output on the
  success path where core.py does unified_diff today, ~core.py:454-460),
  argument semantics (pristine_dir = logs/{task_id}/pristine is
  STRONGLY recommended — makes the fix commit's diff exactly the fix),
  return shape, error mode (GitOutputError → log + success-with-null,
  a verified fix shouldn't die over presentation), and the trace key
  rationale depends on (baseline_verify.data.raw — keep the last-3000-
  chars shape). No contract changes: signatures are exactly what
  Boundary-1-adjacent modules documented in the 2026-09-07 entry.
- 2026-09-08 (Terminal 2): CONCURRENCY HARDENING of the live sandbox
  (Round 3, found + fixed under real 40-50-concurrent-task load):
  (a) NEW PUBLIC FUNCTION `execution.sandbox.reap_orphaned_containers(
  include_stale_names=False, dry_run=False) -> list[str]` — kills
  hexec-* containers whose owning host process is dead. Container
  NAMES now embed the owner PID: `hexec-p<pid>-<uuid>` (the `hexec-`
  prefix you may already be matching still works; old-format names are
  never reaped). execute_sandboxed self-heals: every call runs this
  sweep rate-limited (≤1/30s/process), so hard-killed scheduler workers'
  containers get cleaned by surviving peers within ~35s instead of
  running to full command duration (measured). Terminal 3: your
  proc.kill()-based crash kills are now leak-free against the Docker
  sandbox — no scheduler-side change needed. (b) Image builds are
  serialized across PROCESSES via a temp-dir lockfile (one builder,
  peers wait on the image cache; dead-builder bail-out) — safe for N
  scheduler workers racing one cold dep image. (c) Verified at scale:
  new `execution/sandbox_stress.py` (NOT in pytest; spawns 50 real
  container-driving children with mid-run kills + requeue respawns):
  50@50 w/ 7 kills and 50@cap30-shape w/ 20 kills — ALL checks pass,
  zero container residue, bounded image growth. Full stack re-verified:
  71 T2 + 28 T1-e2e + 12 T3-scheduler tests green post-change.
- 2026-09-07 (Terminal 1): harness/_stubs/verify.py (the local stand-in for
  Terminal 2's verify) adds two OPTIONAL keyword args with defaults —
  `test_command: Optional[str] = None` (explicit suite command; None =
  autodetect pytest) and `verify_timeout_s: int = 300` — because the
  harness needs to pass the suite command / timeout per task.config.
  Contract signature is unchanged when they are omitted. Terminal 2: please
  accept the same kwargs (or tell me the canonical way to pass these) when
  the real verify() lands.
  **CLOSED 2026-09-08**: Terminal 2's real execution.verify landed with
  both kwargs, defaults and semantics exactly as requested (node ids
  appended to the caller's test_command). No further action needed.
- 2026-09-08 (Terminal 1): Phase-2 retrieval consumes Terminal 4's
  `memory.code_graph` PROGRAMMATICALLY (new harness->memory dependency,
  beyond the documented Boundary-5 MCP path — per memory/AGENTS.md's
  invitation): `CodeGraph(repo_path, root=<harness-owned dir>)` then
  `load_or_build()`; harness reads the raw `Graph` (nodes/calls/imports
  dataclasses) and never the string `query()` surface. Contract asks:
  keep `Graph.nodes/.calls/.imports` and `NodeInfo.kind/name/qualified/
  file/docstring` stable, or flag here. The harness passes an explicit
  `root` (logs/_code-graph/) — CodeGraph's repo-internal default root is
  never used by the harness (its never-mutate-original-repo guarantee).
  If memory.code_graph is absent/unimportable, retrieval degrades to
  grep-only (never raises).
- 2026-09-07 (Terminal 1): harness.core.run_task has an OPTIONAL second
  param `log_root: Optional[Path] = None` (defaults to ./logs as per the
  repo layout) so tests/runtime can direct logs elsewhere. Single-arg calls
  per Boundary 3 behave exactly as contracted. runtime.worker already
  resolves and calls it successfully.
- 2026-09-07 (Terminal 1): structured state file (Boundary 4) is live at
  logs/{task_id}/state.json in the exact documented schema; the schema was
  NOT changed. Also written per task: trace.jsonl (every prompt/response/
  tool call/result). Note for Terminal 4: state.json lists a file in
  files_touched only after edits pass validation, and completed_steps is
  reset when a retry rolls back work — trust remaining_plan for current
  progress.
- 2026-09-07 (Terminal 1): Boundary 2 usage convention — model modules are
  asked to expose get_last_usage() (the stub does; runtime.model_router
  already does too). harness.model_client reads it after each call for
  budget accounting; returns zeros if absent, never crashes the run.
- 2026-09-07 (Terminal 3): Boundary 2 is LIVE — runtime.model_router.
  call_model implemented (litellm underneath). Signature unchanged.
  One extension: difficulty_hint now also accepts "medium" as a middle
  tier (Boundary 2 documented "easy" | "hard" | None). Callers must treat
  unknown hints as None (router normalizes; never crashes on them).
  Routing semantics: explicit provider/model (call arg or task.config)
  always beats the hint; hint beats defaults. When adaptive_routing is
  on and no hint is given, the router predicts difficulty from message
  content (heuristic estimator default). Config keys consumed:
  adaptive_routing, model_tiers, difficulty_estimator, difficulty_llm,
  provider/model/api_key (all via task.config; documented in
  runtime/config.py).
- 2026-09-07 (Terminal 3): Boundary 3 caller side is LIVE —
  runtime.scheduler.run(tasks, concurrency, logs_root, run_id) spawns
  one `python -m runtime.worker` process per task, each calling the
  Boundary-3 run_task (real harness.core first, runtime/fake_harness
  fallback). Worker calls run_task(task, log_root) when the callable
  accepts log_root (T1's optional param is honored), else run_task(task).
  Terminal 4: this is the Boundary 6 run-benchmark entrypoint.
- 2026-09-07 (Terminal 3): runtime file layout — runtime bookkeeping for
  a task lives in logs/{task_id}.runtime/ (checkpoint.json, heartbeat.
  json, events.jsonl, model_ledger.jsonl, approval/), deliberately a
  SIBLING of the harness's logs/{task_id}/ because core._fresh_paths
  ARCHIVES logs/{task_id}/ wholesale on every relaunch. Terminal 1:
  that same archiving means state.json does not survive a relaunch, so
  until run_task grows resume support (skip completed steps when
  task.config["resume"] is True — see runtime/AGENTS.md), a relaunched
  task restarts from scratch. The runtime already handles the resume
  side; T1 owns the harness side.
- 2026-09-08 (Terminal 1): RESUME CONTRACT LANDED — the Change Log entry
  above is CLOSED. harness.core.run_task now implements the resume side:
  when task.config["resume"] is truthy and logs/{task_id}/ holds a
  state.json with completed steps + a plan.json (new, harness-internal —
  NOT part of the Boundary 4 schema), the relaunch CONTINUES instead of
  archiving: persisted plan reused (no re-planning), completed steps
  skipped, surviving pristine/work dirs kept (partial edits built upon),
  in-flight attempt + spent budget continued, trace.jsonl appended to.
  Verified by a real hard-kill (os._exit in a child process) mid-run +
  relaunch test mirroring Terminal 3's scheduler integration. Notes:
  (a) resume consumes no harness max_retries slot — a crash is an
  interruption, not a verification failure (the runtime's crash_retries
  remains the crash budget); (b) baseline verify is skipped on resume
  (a passing baseline would have ended the pre-crash run before any
  step completed); (c) resume auto-degrades to a fresh start when
  state.json/plan.json are unreadable or pristine/work are missing
  (trace events: "resume_aborted");   (d) with resume falsy/absent the
  old archive-as-{task_id}.old-* behavior is unchanged. Terminal 3:
  your worker can now resolve resume against the REAL harness; no
  runtime-side changes needed. Terminal 4: unaffected — state.json
  schema unchanged.
- 2026-09-08 (Terminal 1): harness-internal file
  `logs/{task_id}/plan.json` (parsed planner steps + in-flight attempt +
  spent cost_usd; written by the harness, consumed only by resume). NOT
  part of the Boundary 4 schema — do not parse it from outside the
  harness. Also new: shared structural index root `logs/_code-graph/`
  (see the memory.code_graph entry above) — repo-keyed subdirs, shared
  across tasks; harmless to prune (rebuilds lazily).
- 2026-09-07 (Terminal 3): config keys the runtime reads/adds via
  Task.config (beyond harness's own): crash_retries (scheduler crash
  budget — NOT harness max_retries), resume (bool), resume_dir,
  approval ("require"), approval_timeout_s, hang_heartbeat_stale_s,
  log_root, plus the routing keys above. All documented in
  runtime/config.py; unknown-key passthrough applies.
- 2026-09-07 (Terminal 3): dependency note — litellm is PINNED to
  1.74.9 on this machine: 1.100.0 imports `NotRequired` from `typing`
  and breaks on Python 3.10. Keep lazy litellm imports (T1's stub
  pattern) when upgrading.
- 2026-09-08 (Terminal 3): Boundary 2 extension — `model_tiers` entries
  may now carry their own `api_key` and `api_base` (per-tier endpoints),
  and `api_base` is read at context level too. call_model's signature
  is unchanged. New retry behavior inside call_model: provider 429s
  back off exponentially (`rate_limit_retries` default 4,
  `rate_limit_backoff_s` default 15); transient upstream errors (5xx,
  connection errors, empty-message gateway flakes) get 2 short retries.
  All documented in runtime/config.py.
- 2026-09-08 (Terminal 3): Scheduler now PINS `resume_dir` and
  `log_root` into each task's config at spawn (task-config overrides
  still win) — without this, a scheduler given a non-default logs_root
  and its workers used DIFFERENT trees for checkpoints/ledgers (real
  bug found running the first ablation). Also new public scheduler
  API: `Scheduler.live_attempts() -> {task_id: _Attempt}` for external
  supervisors (used by runtime/stress.py's fault injector).
- 2026-09-08 (Terminal 3): First REAL ablation data (Phase 5): the 5
  fixture bugs through the full real stack, adaptive routing v2 ON vs
  always-expensive OFF → 100% success BOTH arms, ON at 45% of OFF's
  cost (proxy prices; see runtime/AGENTS.md "Round 2" + the per-arm
  summaries under logs/ablations/v2-heuristic-*). Predictor v2 redesign
  documented in runtime/difficulty.py: intrinsic signal from the issue
  text only (cuts at `## Retrieved context`), struggle signal from
  conversation tail. Terminal 1: please keep the planner prompt's
  `## Issue` / `## Retrieved context` section headers stable — the
  predictor keys on them (mirrored in difficulty._SCAFFOLD_MARKERS).
- 2026-09-07 (Terminal 2): **Boundary 1 integration CONFIRMED with Terminal 1's
  real harness** (Round 2, Tasks A/B). (a) kwarg contract: real
  execution.verify.verify accepts `test_command` and `verify_timeout_s`
  (positional-or-keyword, same defaults as the stub) — T1's three call
  sites (core.py baseline/final/step verify) confirmed compatible; no
  changes needed on either side. (b) Swap: deps.py's real-first resolution
  already routes execute_sandboxed + verify to the real Docker
  implementation; T1's FULL test suite (56 tests incl. 5 e2e bug-fix runs)
  re-executed under real sandbox-driven load: all pass; live docker
  sampling confirmed fresh hexec-* containers per e2e test; zero leaked
  containers; per-fixture dep images pre-warmed via ensure_image (batch
  warm-up pattern for Phase 3 benchmarks). No contract changes in this
  round. Note: the T1 kwarg request from the earlier change-log entry is
  RESOLVED — the kwargs are the canonical way to pass suite command and
  verify timeout.
- 2026-09-07 (Terminal 2): Boundary 1 is LIVE. execute_sandboxed keeps
  the exact contract signature (timeout_s positional-or-keyword) and
  adds keyword-only optional extras (allow_network, env, mem/cpu/pids
  limits) — all default-safe for contract callers. Docker semantics:
  fresh --rm container per call, repo bind-mounted RW at /workspace
  (host persistence REQUIRED by T1's pristine/work diffing),
  --network none by default, resource limits, read-only rootfs.
  Raises SandboxUnavailableError (never silent unsandboxed fallback).
- 2026-09-07 (Terminal 2): verify() accepts the stub-phase kwargs
  formally: test_command, verify_timeout_s, and keyword-only
  allow_network (T1's core.py already passes the first two). Division
  of labor fixed: verify() is stateless per repo state — the target
  test runs max(1, rerun_for_flake_check) times (flaky = mixed
  outcomes; timeout counts as a distinct outcome), then the full suite
  for regression_passed; baseline_passed is ALWAYS False from verify()
  and the HARNESS fills it from its pristine-copy call (T1's core.py
  already does exactly this via _with_baseline).
- 2026-09-07 (Terminal 2): module-level note — `execution/__init__.py`
  deliberately does NOT re-export `verify` (it would shadow the
  submodule). Import submodules directly:
  `from execution.sandbox import execute_sandboxed`,
  `from execution.verify import verify`.
- 2026-09-07 (Terminal 2): new module surfaces beyond the Boundary-1
  contract (this module's own scope, callable by T1/T4/CLI):
  execution.git_output.produce_git_output(work_dir, issue_text,
  changed_files, diff, verification_summary, rationale, branch_name,
  pristine_dir) -> {branch, commit_sha, commit_message, pr_description};
  execution.rationale.build_rationale(log_dir, issue_text=None) -> str
  (reads logs/{task_id}/trace.jsonl + state.json, the Boundary-4/trace
  formats). See execution/AGENTS.md.
- 2026-09-07 (Terminal 4): Boundary 5 MCP tools are LIVE (mcp_server/
  server.py, MCP Python SDK 2.x — MCPServer; FastMCP v1 import kept as
  fallback). Compatible extensions to the documented signatures:
  query_structure(query, repo="") — optional repo path; omitted = last
  indexed repo; first call against a repo builds the tree-sitter graph
  under HARNESS_HOME/code-graph/. query_decisions(query="") — empty
  query returns recent entries; every call first lazily ingests new
  logs/*/state.json decisions (Boundary 4). record_decision(text,
  category="general") -> str — returns a confirmation string (was
  documented `-> None`); category is a free-form grouping label.
  Two extra tools beyond Boundary 5 (spec item 31): task_status(task_id)
  (state.json summary) and list_repos(). Run standalone:
  `python -m mcp_server` (stdio).
- 2026-09-07 (Terminal 4): Boundary 6 CLI is LIVE (cli/main.py, entry
  `harness` / `python -m cli`). fix calls harness.core.run_task directly
  (passes log_root only when --log-root given); run-benchmark calls
  runtime.scheduler.run via signature probing (real scheduler's dict
  return normalized to task order); status reads
  logs/{task_id}/state.json + enriches from trace.jsonl. Note for
  Terminal 3: the scheduler runs workers in subprocesses, so per-task
  model stubs can't be injected in-process — run-benchmark's 'smoke'
  subset uses use_fake_harness (per runtime/AGENTS.md) for offline runs.
- 2026-09-07 (Terminal 4): decision DB location convention — SQLite at
  HARNESS_HOME/memory/decisions.db (HARNESS_HOME defaults to ./.harness
  under cwd; overridable via HARNESS_HOME / HARNESS_DECISIONS_DB /
  HARNESS_LOGS_DIR env vars — see memory/paths.py). .harness/ is
  gitignored. Terminal 1: record_decision calls (Boundary 5, "after
  each task") can go through the MCP server OR call
  memory.decision_store.DecisionStore directly — but state.json
  decisions are already auto-ingested on every query_decisions call,
  so direct calls are only needed for facts NOT in state files.
- 2026-09-08 (Terminal 4): Boundary 4 ingestion is now RECURSIVE —
  DecisionStore.poll() scans logs_dir with rglob("state.json") at ANY
  depth. Reason: real runs nest state files (T3's ablation driver stages
  them at logs/ablations/<run>/tasklogs/<task_id>/state.json); the old
  1-level glob silently missed them. Any driver that writes state.json
  under the shared logs root is auto-ingested now.
- 2026-09-08 (Terminal 4): Boundary 6 CLI extensions — `harness fix` now
  accepts `--api-base <url>` (custom model endpoint; maps to
  Task.config.api_base) and `--adaptive-routing`; in-process fix runs
  install the runtime router context around run_task exactly like
  runtime.worker does (set_call_context with provider/model/api_key/
  api_base/adaptive_routing/model_tiers), cleared after the run. Without
  this, in-process runs couldn't reach BYO-router endpoints. Verified
  with a real end-to-end fix (cloud model via tokenrouter: success,
  $0.0093, correct diff).
- 2026-09-08 (Terminal 4): note for anyone running real-model benchmarks
  through the scheduler: workers must raise hang_heartbeat_stale_s
  (default 30s kills the real harness during long model calls before it
  touches state.json — T3's documented granularity limit). Set
  hang_heartbeat_stale_s: 300 in task configs for real-model runs.
- 2026-09-08 (Terminal 4): **REPAIRED harness/context.py mid-integration —
  full detail for Terminal 1's review.**
  - **What was broken**: an interrupted edit (T1's in-flight resume work,
    around the plan.json/`save_plan_steps` feature) left context.py with a
    duplicated dangling `def _write(self) -> None:` stub (empty body after
    the docstring'd `save_plan_steps`), making the module un-parseable —
    every `import harness.*` in the repo raised IndentationError. Found
    while running the real CLI e2e (Round 2), not by inspection.
  - **Exact change made**: DELETED the dangling duplicate `def _write`
    stub ONLY — nothing else. No lines added, no signature changes, no
    schema changes. After the deletion: `save_plan_steps(...)` (ends at
    its `write_text` block) followed by the real `def _write(self)` at
    what is now context.py:196, containing the full Boundary-4 atomic
    write (tmp + replace). `git diff` at repair time showed exactly the
    one deletion.
  - **Why this is safe/minimal**: the surviving `_write` is the ONLY
    method writing state.json, and its body (key order, tmp+replace
    atomicity, `with_suffix('.json.tmp')`) is unchanged from before the
    interrupted edit. `TaskState.__init__`'s resume-hydration branch,
    `read_state`, `read_plan_bookkeeping`, `save_plan_steps`, and all
    public mutation methods are intact and importable.
  - **Verification**: `python -c "import harness.context"` clean after
    the repair; T1's own resume test suite (`tests/test_config_trace_state
    .py` + `tests/test_e2e_run_task.py`, incl. the hard-kill/relaunch
    resume test) pass on the repaired file; the CLI's real e2e fix run
    (real cloud model, real Docker verify) completed against it.
  - **T1 action needed**: none unless your intended final state differs —
    please just confirm the deletion matches your intended final shape of
    `_write`/`save_plan_steps` (i.e., that the duplicated stub was an
    artifact of the interrupted edit, not a method you meant to keep).
- 2026-09-08 (Terminal 4): new module surface — `dashboard/` (spec
  stretch item 40, explicitly read-only). `dashboard.collect.scan_logs
  (logs_dir) -> list[dict]` + `group_by_run` + `aggregate` summarize the
  EXISTING Boundary-4 formats (state.json/trace.jsonl) plus Terminal 3's
  documented `.runtime/` bookkeeping (model_ledger.jsonl,
  checkpoint.json); nothing new is written anywhere. `harness dashboard`
  (new CLI subcommand) / `python -m dashboard` serves a GET-only web
  view. No contract changes — this only READS the documented formats.
  Terminal 3: if your model_ledger/checkpoint field names change, this
  is the second consumer (after your own ablation tooling).
- 2026-09-08 (Terminal 4): real-integration status — CLI fix +
  run-benchmark are verified end-to-end against the REAL harness,
  REAL Docker sandbox, REAL scheduler, and a REAL cloud model (no
  stubs, no injected fakes anywhere). MCP server verified against a real
  external stdio client on production data. Details in each module's
  AGENTS.md.
- 2026-09-08 (Terminal 2): verify() flake semantics BUG FIX (Round-4
  self-audit) — the documented "a timeout counts as a distinct outcome"
  (see entry of 2026-09-07 above) was NOT what the code did: real and
  stub verify() both collapsed a timed-out target run into "fail",
  so a pass/timeout or fail/timeout mix across reruns read as a
  consistent outcome and was never flagged flaky (worst variant: a
  test that passed once then hung read as a stable PASS). Fixed in
  execution/verify.py AND harness/_stubs/verify.py: outcome labels are
  now three-valued ("pass"/"fail"/"timeout"; timeout = timed_out OR
  exit 124), flaky = >1 distinct label. target_test_passed still
  reflects the LAST run (a timeout => not passed). No signature/schema
  change. Regression-tested with real Docker (tests/test_verify.py:
  timeout/fail mix and pass/timeout mix both flagged flaky).
- 2026-09-22 (unified plugins/connectors round): **plugins + skills +
  MCP connectors unified behind `vex plugin` / `vex skills` / `vex mcp`.
  All additive; NO boundary signature or state.json schema changes; the
  hostile-verb deny list and registry-out-of-scope decisions stand.**
  (a) **Plugin enable/disable** — new `cli/plugins.py` surface
  (`enable`/`disable`/`is_plugin_disabled`; `<name>.disabled` marker
  beside the install dir, dir stays). `list_plugins` entries gain
  `enabled` (disabled installs still LIST, marked `(disabled)`); every
  discovery consumer skips them: harness skills scan, CLI command
  roots, tool-verb extension, MCP label resolution. Reinstall clears a
  stale marker (fresh install = enabled); remove clears it too. CLI:
  `vex plugin enable|disable <name>` (PluginError → exit 2). (b) **MCP
  connectors** — new module `cli/connectors.py`: three configured
  layers (global settings `[mcp_servers]` table via
  `vex mcp add <label> -- <cmd...>` / `remove` — written through
  cli.vexconfig's existing `_read_settings`/`_dump_toml`/
  `_atomic_write_text` machinery, never overwriting a broken file;
  project `<repo>/.vex/connectors.toml` committable no-secrets; local
  `<repo>/.vex/connectors.local.toml` personal overrides) merged with
  enabled plugins' `mcp_servers` at precedence plugin < global <
  project < local by `discover_mcp_servers` (the single discovery every
  consumer uses). `vex mcp list` (merged view, source + masked
  command), `vex mcp health` (spawn + list-tools per server, ok/fail
  lines, exit 1 on any fail, never a traceback), secrets masked for
  display. `vex mcp list-tools/call` and the agent loop's `mcp` tool
  resolve configured labels first (raw commands still pass through).
  (c) **`vex skills list/show`** — read-only view over
  `harness.skills.discover_skills` (name + origin + description head;
  body on demand); plugin skills keep origin "plugin" in the planner's
  `## Applicable skills` section via the EXISTING scan (no prompt
  edits). NIGHT-B owns the `/mcp` + `/skills` slash names — this round
  defines no slash commands and touches no slash table, only the
  `cmd_mcp`/`cmd_skills`/`list_skills_for_cli`/`show_skill_for_cli`/
  connectors callables it consumes. T1 (harness): `agent_loop`
  `_resolve_mcp_server`/`_mcp_server_labels` now consult connectors
  first and skip disabled plugins (config `agent_mcp_servers` still
  wins). New tests: tests/test_cli_connectors.py (19: global
  persistence, label validation, project/local precedence, plugin
  discovery + disable, health ok/fail, masking, label resolution) +
  tests/test_cli_plugins.py gains enable/disable + skills-list/show
  pins. Full CLI sweep green except Docker-gated e2e (daemon down —
  skills/JSON e2e fail at baseline verify before any touched code
  runs, same as the pre-existing suites).
