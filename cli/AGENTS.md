# cli/ — Terminal 4: User-Facing CLI (Boundary 6)

## Unified plugins/connectors round (2026-09-22) — enable/disable, `vex mcp` registry, `vex skills`

*(Unifies the scattered pieces — SKILL.md auto-injection, plugin
bundles, `vex mcp list-tools/call`, `.vex/commands/` — into one
Claude-Code-connectors-style surface. CLI + registry only; no prompt
edits, no slash-table edits (NIGHT-B owns `/mcp` + `/skills` and
consumes the callables below), no eval changes. See the INTERFACES.md
Change Log entry.)*

### What shipped

- **Plugin enable/disable** (`cli/plugins.py` + `vex plugin`):
  `disable` writes a `<name>.disabled` marker beside the install dir
  (dir stays); `enable` removes it. `list_plugins` entries gain
  `enabled` (disabled installs still list, marked `(disabled)`);
  skills/commands/tool-verbs/MCP discovery all skip disabled plugins
  (markers checked directly in `harness/skills.py` +
  `cli/commands.py`, so even manifest-broken installs mute cleanly).
  Reinstall clears a stale marker; remove clears it too.
- **NEW `cli/connectors.py`** — the unified MCP registry: global
  settings `[mcp_servers]` (`vex mcp add <label> -- <cmd...>` /
  `remove`, written through `cli.vexconfig`'s existing
  `_read_settings`/`_dump_toml`/`_atomic_write_text` — a broken global
  file is refused, never overwritten) + project
  `.vex/connectors.toml` (committable, no secrets) + local
  `.vex/connectors.local.toml`, merged with enabled plugins'
  `mcp_servers` at plugin < global < project < local by
  `discover_mcp_servers`. `vex mcp list` (source + masked command),
  `vex mcp health` (per-server ok/fail, exit 1 on any fail, never a
  traceback), secrets masked. `list-tools`/`call` resolve labels
  first; the agent loop (`harness/agent_loop.py`) resolves through
  connectors too and skips disabled plugins.
- **`vex skills list/show`** (`cmd_skills` + `list_skills_for_cli` /
  `show_skill_for_cli` for NIGHT-B): name + origin + description head,
  body on demand. Plugin skills keep origin "plugin" in the planner
  prompt via the existing scan (verified: marker + header land in the
  scan block; no prompt code touched).

### Verification + honest notes

- tests/test_cli_plugins.py extended (enable/disable roundtrip +
  discovery/tool-verb pins, reinstall/remove marker clearing, CLI
  roundtrips + exit-2 errors, skills list/show incl. disabled-hidden)
  and NEW tests/test_cli_connectors.py (19: persistence, validation,
  precedence, plugin+disable, health ok/fail, masking, label
  resolution) green; adversarial 62/62 intact.
- 6 failures in the wider sweep are Docker-daemon-down environmental
  (daemon unreachable machine-wide — baseline verify refuses before
  any touched code runs): 3 skills REAL-loop e2e + 3 release --json
  e2e. Re-run when Docker is back. Bare-repo e2e done with the
  daemon-independent path: install → skills listed → scan block
  carries the marker with origin plugin → health ok → disable →
  marker gone.
- Two rich-markup gotchas fixed live: `[disabled]`/`[mcp_servers]` in
  `con.print` strings parse as style tags and vanish — user-visible
  state uses `(disabled)` parens; `<`/`>` in muted lines likewise
  avoided. `[[...]]` escaping is NOT reliable on this rich version
  (renders `[]`).
- Not built: registry/marketplace (still out of scope); project-tier
  `vex mcp add --tier` (global-only by design — project/local layers
  are hand-edited committable files).

## Slash-surface round (2026-09-22) — /init /model /login /logout /mcp /skills /cost /undo /clear

*(Surface wiring only: the agent loop + session.py exist; every new
slash calls an existing engine and renders in both shells. No
harness/runtime/memory contract changed — see the INTERFACES.md
Change Log entry.)*

### What shipped

- **9 new built-ins, REPL + TUI**: `/init` (scaffold .vex/ via the
  vexconfig writers — called, never reimplemented), `/model`
  (already existed; now also in the builtin guard set), `/login`
  (REPL: onboard.cmd_login incl. --tier; TUI: the existing
  _OnboardScreen modal) + `/logout` (onboard.cmd_logout — no new auth
  code anywhere), `/mcp [label]` (servers from config
  agent_mcp_servers + plugin manifests; tools via the public
  list_mcp_tools, label resolution via the agent loop's read-only
  _resolve_mcp_server — harness/agent_loop.py untouched), `/skills
  [filter]` (discover_skills + origins), `/cost` (last run + session
  total from trace usage-sums), `/undo [file|all]` (alias of
  /diff undo through one shared core), `/clear` (fresh conversation
  file, old kept on disk; the REPL loop re-syncs its cached handle).
- **One core, two idioms**: undo_result/history_matches/cost/skills/
  mcp/init/clear helpers in cli.interactive with a `say(markup)` hook
  (REPL: console; TUI: transcript). /diff undo refactored onto
  undo_result (messages byte-identical both shells); /history reuses
  the session_matches grammar (a history line scores as a session
  record's issue text — key:value tokens narrow honestly).
- **Shadowing**: BUILTIN_SLASH_COMMANDS gains all 9 (+ /model, which
  was missing — a cost.md would have listed as custom); load_command
  guard untouched; _HELP + palette entries updated; cli/fuzzy.py
  untouched (new palette entries rank through the existing matcher).
- **Live discipline**: read-only newcomers work mid-run in both
  shells (reader thread + in_flight branches); mutating ones
  (/init /login /logout /clear /undo, /model pin, /mcp + label) refuse
  honestly mid-run. Unknown args → usage hint, never a traceback.
- Forbidden surfaces respected: ui.py untouched, no CSS/color
  changes, plugins.py untouched, vexconfig only called.

### Verification + honest notes

- NEW tests/test_cli_slash2.py **33/33** (every slash live-driven in
  both shells: REPL dispatch + Pilot transcript asserts + palette
  fuzzy finds + helper units + hostile-input pins).
- Full CLI sweep: everything green EXCEPT 6 Docker-down failures
  (daemon unreachable — `docker_available()` False; all fail at
  baseline verify with 0 attempts/0 model calls before any touched
  code runs: test_cli ×2, test_cli_vex scripted-fix ×1, release
  JsonMode trio ×3). One further flake observed once:
  test_cli_plugins enable/disable roundtrip failed inside one combined
  ordering but passes standalone (42/42) and in re-runs — those tests
  drive the REAL plugins root (no HOME isolation), so it is
  order/state-dependent pollution, not this round's code
  (cli/plugins.py + cli/main.py untouched here).
- ruff: commands.py + test file clean; interactive.py/tui.py hold at
  their pre-existing baselines (all flagged lines are pre-existing
  regions; the 3 findings on new lines were fixed).
- Known races/limits: REPL /login contends with the reader thread
  for stdin (same pre-existing race as plan-preview input); TUI
  /mcp + label spawns a server on the UI thread (refused in-flight,
  inline when idle); /history status:-style filters match nothing
  (history carries no status — honest narrowing, documented).

## Crimson-on-black re-theme round (2026-09-22) — pitch-black + crimson brand, warm-grey look gone

*(Visual-only round: `cli/ui.py` tokens + `_VEX_RAMP`, `cli/tui.py` CSS
literals/prose, `VEX_DESIGN_SYSTEM.md` contract, theme pins in
`TestOxbloodTheme` + `TestDesignSystem`. No slash tables, plugins,
session logic, prompts, glyphs, spinners, jokes, or bell touched.)*

### New locked palette (all measured, not eyeballed)

- `bg-base` `#000000`, `bg-panel` `#0A0A0A`, `bg-panel-hover` `#161616`,
  `border-subtle` `#2A2A2A` — pitch black + neutral greys, zero warm undertone.
- `accent-text` `#E8114A` — crimson one step brighter than `#DC143C` in the
  SAME hue: `#DC143C` is ~4.2:1 on black (below the 4.5:1 bar), `#E8114A`
  is ~4.6:1 (brightness adjusted, hue kept). Fill weight `#4A0A14` (under
  `#F5F5F5` text only), glow/rose `#FF7A93` (~8.5:1, thinking spinner +
  diff file headers), `text-primary` `#F5F5F5`, `text-secondary` `#8A8A8A`
  (neutral, never warm). Success/error/warning unchanged (semantic).
- Wordmark ramp re-tinted crimson→rose→white-hot:
  `("#7A0C26", "#E8114A", "#FF7A93", "#FFF0F3")` — ANSI-shadow
  letterforms + 26-col geometry byte-identical (letterform pins held);
  per-column blends are pinks, never orange. Ember ◆ unchanged glyph, re-tinted.
- Oxide-orange `#D98E5F` gone from ALL chrome (zero occurrences in `cli/*.py`
  outside history docs); old warm hexes (`#D8A47F #A89490 #0A0808 #C9504C …`)
  likewise gone from source. Logo interpolation stops are the documented
  exemption (new ramp emits none of them anyway).
- Diff bands re-tinted near-black: add `#0A1510`, del `#1C0A0F` (was warm `#26100F`).

### Verification

- tests/test_cli_vex3.py **49/49**, tests/test_cli_tui.py **50/50**
  (incl. the documented order-sensitive `test_run_line_updates_in_place`
  worker-teardown flake — passes in isolation and in the file-alone run).
- SVG audit `Temp/opencode/drive_tui_crimson.py` (prior round's compliance
  pattern): **hero/live/done 3/3 COMPLIANT**, zero unexplained colors, zero
  banned old-chrome colors, 26 logo blends with zero orange leak. Old
  oxblood SVGs kept as `vex_tui_{hero,live,done}.svg`; new screens as
  `vex_tui_{hero,live,done}_crimson.svg`; side-by-side color tables in
  `tui_crimson_report.json`.
- `TestColorControl` 3/3 (NO_COLOR/--no-color strip ANSI); `--json` doc
  stays byte-clean + parseable. `TestJsonMode` trio fails ONLY on the
  documented Docker-down environmental cause (attempts 0 before any model
  call — daemon unreachable machine-wide); the JSON payload itself is clean.
- ruff clean on all touched files (`cli/tui.py:1434` I001 is a parallel
  session's `cli.session` import block — pre-existing, untouched).
- cp1252 fallbacks (`GLYPHS`/`SPINNER`/`THINK_FRAMES`), `JOKES`, `bell()`
  verified untouched via diff (content matches prior rounds exactly).

## Agent demo-parity round (2026-09-21) — resume keeps talking, diff parity, demo

*(Interaction parity for the 60s demo: ask -> plan -> approve -> edit
-> undo -> resume -> compact, video-able in TUI+REPL. No fix-mode
changes — the flag path still drives harness.core.run_task directly.)*

### What shipped

- **Agent resume keeps talking**: `_resume_task` returns the run's
  result dict (agent branch: same task id, pristine/orig kept, prior
  trace + conversation turns replayed via the new `resume_history`
  kwarg on `_run_one_agent`; fix branch: `_execute_task`'s dict).
  REPL `/resume` folds it via `_fold_resumed` into `last` + the
  conversation transcript; TUI `_start_resume` records the /resume
  user turn and the resume worker folds the dict via `_note_result`
  (previously discarded). Legacy `_run_one_agent` fakes without the
  kwarg fall back without replay (TypeError guard).
- **TUI /diff parity**: empty `last["diff"]` recomputes the live
  agent diff from pristine/ (the REPL already did); undo-missing
  names stay honest; transcript+sidebar update via the existing
  `last["diff"]` refresh.
- **No-traceback guards**: both shells wrap slash dispatch so
  hostile/garbage input (`/diff undo ../../x`, NUL bytes, bad
  `/trace` args) degrades to honest lines, never a traceback
  (`/cancel` excluded — it is SIGINT semantics by design).
- **demo/agent_demo.py**: offline 7-step scripted demo (question,
  @mention, plan, require-mode approval, diff/undo/copy-diff,
  resume replay, compact) — 0.2s, exit 0, artifacts under
  demo/demo-work-agent/.

### Verification

- tests/test_cli_session.py 21/21 (bad-input matrix, _fold_resumed,
  agent-resume replay incl. same-id + history content).
- tests/test_cli_tui.py TestAgentDiffParity (live recompute,
  missing-undo honesty, bad-line safety) + TestAgentApprovalModal
  unchanged green.

## First-run onboarding round (2026-09-21) — no model set -> wizard, once

*(The "no litellm auth error mid-run" objective: `vex` with no model
configured offers the inline wizard THERE, saves it, never asks
again. Claude Code's `/login` + OpenCode's wizard, adapted for Vex's
any-router world: free-text base_url + model name, never a
hardcoded-only provider list. CLI-internal + test-hygiene only; see
the INTERFACES.md Change Log.)*

### What shipped

- **New `cli/onboard.py`** — PRESETS (Official: OpenAI/Anthropic/
  Gemini via litellm names; Router: OpenRouter/TokenRouter/Ollama/
  Custom), detection (`needs_onboarding` over flags > env > local >
  project > global > legacy; Ollama-loopback needs no key),
  `run_repl_wizard` (pick -> editable base_url -> model with 2-3
  suggestions + free text -> masked api_key via getpass -> ONE tiny
  live litellm call; fail = honest error + retry, bad creds NEVER
  saved), save discipline (api_key+base_url ALWAYS global; model
  global unless `--tier project`; official clears stale router
  base), `vex login [--tier]` / `vex logout` (strip key only) /
  `/model` (effective model + source tier), flag-command gate
  (`missing_credentials_exit`: stderr + exit 4, never prompts;
  --json prints a parseable error doc; fake/mock/scripted models
  exempt so offline suites keep working).
- **TUI `_OnboardScreen`** (cli/tui.py: stepped modal — OptionList
  pick, Input base/model/masked-key, worker-thread live test, Esc
  skips). Auto-pushed once from `on_mount` via an explicit
  `onboard_prompt=True` passed by `run_tui` — NOT a mount-time
  isatty probe (textual swaps sys.stdout under Pilot, so the probe
  fired in headless drives and ate 26 tests' input; the flag keeps
  direct VexApp(...) construction modal-free). Async step painter
  (mount-then-focus; OptionList pre-highlighted so Enter works).
- **Secrets** (`cli/vexconfig.py`): project-tier `api_key` writes
  refused (would be committed — global or `--tier local` instead);
  settings writes chmod 600 best-effort POSIX (append path too).
  Masking (`sk-...<last4> (set)`) and `config list` source labels
  already existed — now pinned by test.
- **Session hooks**: REPL `maybe_onboard_repl` after the chain
  load (once, pipe-safe); `/model` in both shells (+ palette
  entry); `login`/`logout` in the parser metavar + dispatch.

### Verification + honest notes

- tests/test_cli_onboard.py **40 collected (39 pass + 1 POSIX-chmod
  skip on Windows)** green, incl. the TUI modal full-save flow
  through Pilot and the wrong-key-saves-nothing + retry-fix pins.
- Full CLI sweep: everything green EXCEPT the Docker-down
  environmental set (daemon unreachable machine-wide — trace-proven
  `baseline verify crashed: docker daemon not reachable` before any
  touched code runs): offline fix e2e, issue-from-file, scripted
  interactive fix, JsonMode trio. Re-run when Docker is back.
- Two pins updated for the exit-4 contract (intents unchanged):
  test_cli_errors crash-classification (dummy creds so it reaches
  the crash -> still 3) and the adversarial injection matrix (4
  allowed — gate means the payload runs even less).
- ruff: `cli/onboard.py` + tests + `cli/vexconfig.py` clean; tui.py
  holds at its working-tree state (my RUF006 pair fixed; the one
  remaining I001 is a parallel session's `cli.session` import block
  at on_mount, untouched by this round — flagged, not fixed).

## First-run .vex/ scaffold round (2026-09-21) — `curl|bash` -> `cd repo; vex` just works

*(The "install-to-first-vex" objective: one-liner installs the package
ONLY; the first `vex` inside a git repo scaffolds `<repo>/.vex/` with
examples, editable like Claude Code/OpenCode. No Boundary changes —
CLI-internal + test-hygiene only; see the INTERFACES.md Change Log.)*

### What shipped

- **Auto-scaffold** (`cli/vexconfig.py::maybe_scaffold_repo`, called
  from both session entries after `ensure_first_run`): inside a git
  repo (nearest `.git` ancestor via `find_git_root`; `$VEX_PROJECT_DIR`
  overrides to its parent) creates what's missing under `.vex/` —
  `settings.toml` (comment-only committable starter, zero keys),
  `settings.local.toml` (comment-only, zero keys so effective settings
  never change), `commands/fix.md` (`$ARGUMENTS` template; `/fix` is
  not a built-in so the file is live — a `review.md` example would
  have hijacked `/review`'s builtin diff view), `skills/code-review/
  SKILL.md` (frontmatter + body, discovered by the harness scan).
  Example dirs are created ONLY when the dir itself is new (a deleted
  example stays deleted). Never overwrites, never outside a repo,
  never raises (read-only checkouts stay usable); one muted
  `repo setup:` notice only when files were created. `ensure_project`
  keeps its `(created, path)` signature (now full layout);
  `ensure_project_layout` returns the created list;
  `vex config init-project` reports the extras.
- **Warn-once** (`_warn_once`): broken/unreadable/wrong-typed tiers
  warn on first read per process — `config list` re-reads the chain
  per key and spammed one warning per key before.
- **Installers verified package-only** (no changes needed): no config
  writes anywhere in install.sh/.ps1/.cmd; banners already agent
  wording; python>=3.10 hard-fail, git required only for git-URL
  sources (warn-only on PyPI), Docker warn-only, idempotent PATH,
  post-install `vex --version` + `vex update --check`.

### Verification + honest notes

- tests/test_cli_config.py **52/52** (10 new `TestProjectScaffold`:
  full layout + loaders roundtrip, subdir lands at root, no-overwrite,
  no-outside-repo, VEX_PROJECT_DIR override, never-raises, init-project
  parity, real `git status -uall` pin, warn-once).
- Neighbor suites: vex3 + plugins + modes 176+2skip; tui + agent_loop
  78 (run before a parallel session's 22:01-22:12 edits to
  cli/tui.py + cli/interactive.py + harness/agent_loop.py); ruff clean
  on all touched files (main/interactive/tui findings are the
  documented pre-existing baselines, none on new lines).
- **Post-close note: test_cli_tui.py shows 14 failures AFTER those
  22:01-22:12 parallel edits** (mode-dispatch/feed/todo classes, e.g.
  `ran["mode"]` None where the migration's new `_mode_worker
  ("agent_task", ...)` dispatch no longer hits the old
  `_run_one_agent` monkeypatch). That dispatch code is untouched by
  this round (my tui.py edit is run_tui-only; failing tests build
  VexApp directly) — the migration session's pins to update, flagged
  per cross-terminal practice, not reverted here.
- **Two self-inflicted tree pollutions, both reverted and then pinned
  by test design**: a smoke test scaffolded `C:\Users\pavan\.vex` +
  home `.gitignore` (walk-up found the home dotfiles git repo —
  removed both, restored); the first `never_overwrites` draft
  scaffolded the real repo root (missing chdir — removed `.vex/`,
  kept the pre-existing `.gitignore` hunk). Session-entry test
  drivers (vex3, modes) now `chdir(tmp_path)`; agent_loop already did.
- Known edge: a home-dir dotfiles git repo counts as "a repo" for
  walk-up (consistent with the existing project-tier discovery, not a
  new rule).

## General-agent session round (2026-09-21) — `vex` is a daily-use agent

*(The harness engine is documented in harness/AGENTS.md — the loop, the
tools, the trace contract, and the Task-C guarantees. This section
covers the CLI surfaces. Built on top of the agent-session round's
persistent sessions work also in flight in this tree.)*

### What the user sees / types

- **Any plain sentence just works**: "explain how routing works" answers
  read-only (the unchanged `_run_one_question`); "add logging to X",
  "run pytest ... and fix failures", "refactor ...", bug reports — all
  run ONE agent loop on the LIVE repo (`_run_one_agent`), with the
  answer + diff + cost rendered at the end. "hi"/thanks/meta → inline
  reply, nothing launched (the original hi-bug contract holds).
- **`/diff`** re-renders the last diff; **`/diff undo`** reverts the
  last agent edit (`/diff undo all` reverts everything the task
  changed). Undo is for agent sessions only — fix runs never touch the
  live repo, so there is nothing to undo there (said honestly).
- **Mid-run steering is unchanged**: plain text while a run is live
  steers via the same `_LIVE_RUN` + `steering.jsonl` journal (the agent
  loop polls it every turn); `/steer`, `/cancel`, `/status`, `/quiet`
  all work mid-agent-run.
- **`/resume <agent-id>`** restarts the agent loop under the same task
  id from the current tree (diff/undo references kept); fix-task resume
  still goes through the checkpoint contract.
- **Deliberately unchanged**: `vex fix` (flag path → `run_task`,
  verifier-gated), `/plan <text>` (preview lives in `_execute_task`),
  `/review` with a resolvable template (the review-then-fix plugin
  example stays verifier-gated), `/approve` + `/reject` (the worker
  file-gate protocol). Generic `/<custom>` templates now run as agent
  tasks (they are arbitrary reusable instructions).

### Wiring (REPL + TUI share it)

- Dispatch is `harness.agent_loop.classify_agent_input` (question |
  agent_task | chit_chat) — `harness.router.route_kind` and
  `cli.intent` are no longer consulted by the session (both stay for
  their pinned unit tests and programmatic callers).
- `_run_one_agent` mirrors `_execute_task`'s session core (LiveMonitor
  + `_fire_task_start` + `_set_live_run`/`_clear_live_run` + router
  context + `record_session` + `notify_done`) around `run_agent`,
  plus the `agent_approval=require` console approver
  (`_agent_approve_prompt`). The TUI's `_agent_worker` drives it
  through the same capture/modal machinery as the other workers
  (require-mode currently refuses there — no modal yet, safe default).
- Feed/sidebar/card need no changes: the agent trace speaks the same
  kinds (tool_call verbs classify in `cli.tracelog`, incl. the new
  READ/GLOB/GREP/EDIT/WRITE/MEMORY/VERIFY/DONE renderings, plus
  `edit_applied`/`approval_*` entries).

### Verification at close

- tests/test_agent_loop.py (32) + updated session-wiring suites:
  test_modes TestSessionWiring (3-way), test_cli_vex3 bug-sentence
  (-> _run_one_agent), test_cli_tui (dual-installed fakes,
  agent-dispatch test, direct fix-worker preview tests, agent mode
  panel pin), test_cli_plugins generic-custom-command (-> agent).
- Full TUI file 43/43; CLI sweep green except Docker-down
  environmental failures (daemon unreachable machine-wide — baseline
  verify fails before any session code runs; re-run when back).

### Honest notes / known limits

- The question path is repo-grounded Q&A without web fetch — external
  research questions get general-knowledge answers labeled as such.
  (The agent loop itself has a read-only `fetch` tool for docs/errors.)

## Agent trust round (2026-09-21) — TUI approval modal, agent plan preview, undo hardening

*(Harness side documented in harness/AGENTS.md — tool_router, fetch/
MCP/plugin tools, render_agent_plan, undo events. This section covers
the CLI surfaces.)*

- **TUI `_agent_approve_fn`** (cli/tui.py): require-mode agent tools
  raise the SAME confirm-modal pattern as plan-preview/approval (diff
  + command body, y=once / a=always-latched-per-run / n=safe-default
  on Esc), with a VEX_NOTIFY bell. `_agent_worker` passes it plus any
  pending plan guidance; the call is signature-inspected so legacy
  `_run_one_agent` fakes (test scaffolding) keep working.
- **Agent plan preview**: `/plan <text>` classifies — agent-shaped
  text gets the lightweight preview (REPL: `_agent_plan_preview`
  input flow with approve/edit-steer/cancel; TUI: steps/files
  transcript + confirm modal, e=edit opens a steering prompt whose
  text steers the run). Bare `/plan` still toggles preview for the
  next run. Approved plans inject as guidance, never a contract.
- **`/diff undo <file|all>`** (both shells): restores exactly that
  file (missing names reported honestly); re-renders the live diff.
- **Verification**: test_cli_tui 47/47 (+4 TestAgentApprovalModal
  latch unit); REPL preview covered in test_agent_loop
  TestAgentPlanPreviewREPL; ruff parity on interactive/tui (no new
  findings vs pre-existing baselines).

> **Cross-terminal note (2026-09-09):** the sections below through Round 6
> are Terminal 4's. The "Vex CLI pass" section at the BOTTOM is from a
> different session (Terminal 2, the execution/terminal), which did the
> rename + rich + interactive work described there — coordinated via this
> file and the INTERFACES.md Change Log, building ON TOP of Round 6's
> adversarial hardening (all 62 adversarial tests kept green; only three
> decorative output-string assertions in test_cli.py were updated to the
> new render format, same verification intent).

## Agent-session round (2026-09-21) — persistent conversation, slash parity, memory-first

*(`vex` feels like opencode/claude: one conversation file per session,
@file mentions, /plan + /review + /compact + history, memory queried on
start and ingested on finish. Built ON TOP of a parallel session's
in-flight agent-loop migration (`harness.agent_loop`,
`classify_agent_input` chit_chat/question/agent_task, `_run_one_agent`)
— that migration owns the two stale pins noted below; this round
adapted to its dispatch rather than fighting it.)*

### New module `cli/session.py` (the whole round's state lives here)

- **One state file per conversation** (`<log_root>/_conversations/
  <sess-id>.json`: transcript turns + raw input history + compacted
  summary — atomic tmp+replace writes, `errors="replace"` reads).
  Both shells load-or-create on start and record every turn
  (REPL: convo/question/research/build/agent branches; TUI:
  `_start_run` + `_note_result` + chit_chat inline).
- **`expand_at_mentions`** — `@path` resolution (exact relative path,
  then unique basename/endswith, then `cli.fuzzy` rank) + capped
  first-lines snippet appended as `@path context` (binary skipped,
  unknown tokens verbatim, total on garbage).
- **`compact_session`** — deterministic summary of dropped turns +
  enrichment via the EXISTING `TraceLogger.find_events` recall
  primitive (no new mechanism); keeps the last 12 turns.
- **Memory-first, no manual `vex memory` calls**: `session_memory_brief`
  (repo-scoped recent decisions + persisted code-graph file/symbol
  counts, load-only so session start stays instant — shown as muted
  lines on REPL start and in the TUI transcript) and
  `ingest_session_facts` (Boundary-4 `poll` + one `session`-category
  row; called from `record_session`, so every finish/interrupt lands).
- **`copy_text_to_clipboard`** — `clip`/`pbcopy`/`xclip`/`xsel`,
  False when unavailable (callers show the text instead). All public
  functions total (never raise), ASCII-safe sources (only U+2014 in
  docstrings, matching repo practice).

### Slash parity (both shells, same semantics)

- **`/plan [text]`** — bare toggles `state["plan_preview"]`; with text
  forces preview for that run (the backend watcher becomes a modal in
  the TUI, a prompt in the REPL — approve before edits).
- **`/review`** — bare renders diff + rationale together (new
  `_render_review` REPL / `_render_review` TUI using the shared
  `last_rationale_text`); `/review <args>` WITH a resolvable
  `review.md` runs that template (the plugin example keeps working —
  `/review` deliberately stays OUT of `BUILTIN_SLASH_COMMANDS`).
- **`/resume`** with no id resumes the most recent resumable session
  (None found = the old usage line — the existing usage test holds).
- **`/compact`** (recall-backed summary, recent kept),
  **`/copy-diff`** (+`/copy` alias), **`/history [query]`** (REPL
  print; TUI transcript).
- `BUILTIN_SLASH_COMMANDS` gains `/plan /compact /copy-diff /copy
  /history /trace /feed /steer` (custom names can no longer shadow
  them; INTERFACES.md Change Log entry filed).

### History + @ autocomplete

- REPL: readline history file (`logs/.vex-input-history`, best-effort)
  + `/history`; TUI: Up/Down browses (`VexApp.on_key`, main-input only
  — modal filter boxes keep their keys), **Ctrl+R** opens the
  searchable `_HistoryScreen` (enter recalls), **Ctrl+Space** completes
  the @fragment under the cursor from `scan_repo_files` (fuzzy).
  Palette gains /plan /review /compact /copy-diff entries.
- Feed reasoning/action styling untouched (italic-dim vs bold-accent);
  `ui.bell` + `VEX_NOTIFY=0` untouched (already correct — one ring per
  finished task via TUI `_finish_run` / REPL `notify_done`).

### Verification + honest notes

- NEW tests/test_cli_session.py **18/18** (state roundtrip/corrupt,
  expansion matrix incl. binary, compaction, ingest row lands in an
  isolated decisions DB, all new slashes incl. custom-/review
  coexistence, hostile-input no-traceback).
- test_cli_adversarial **62/62**, vex2+errors **37/37** green post-change;
  ruff clean on session.py/commands.py; interactive.py back at its
  15-finding baseline (my one I001 fixed, rest pre-existing).
- **NOT MINE (parallel session's in-flight migration owns these)**:
  `test_slash_custom_command_without_arguments` (monkeypatches
  `_run_one_fix`; generic custom path now calls `_run_one_agent`) and
  TUI `TestPlanPreviewModal` x2 (`app.screen` never becomes
  `_ConfirmScreen` — "fix ..." lines now route to `_agent_worker`,
  which has no plan-preview watcher; the card-numbers test passes in
  isolation — the third failure in that file is the documented
  worker-teardown flake class). Full TUI file: 40/43 with those two
  excluded. Their migration, their pins to update — flagged in the
  INTERFACES.md entry rather than reverted here.

## Mid-Task Interactive Steering round (2026-09-14) — steering from the REPL/TUI

*(The harness-side mechanism is documented in harness/AGENTS.md — the
journal, the loop's consume points, the state-machine interaction, and
the Task-C guarantees. This section covers the CLI surfaces.)*

### What the user sees / types

- **REPL**: while a run is live, ANY plain-typed text (that is not
  conversational — `cli.intent.classify` gates chit-chat, which is
  answered inline and never injected) becomes a steering instruction:
  plain text → guide; `replan: …` / bare `replan` → re-plan at the next
  step boundary; `abort` / `abort: reason` → clean resumable stop.
  `/steer <text>` is the explicit form (usage hint on empty). Slash
  commands still work mid-run (`/status` `/diff` `/cancel` `/quiet`
  `/approve` `/reject` `/sessions` `/trace`); `/resume` and custom
  commands explain that a run is in flight (never silently swallowed).
- **TUI**: same semantics — in-flight plain text and `/steer` route
  through `_steer_live`; acks render INTO THE TRANSCRIPT via
  `steer_live_run(..., say=self.transcript)` (the TUI's console output
  is captured away, so the ack hook is required, not decorative).

### How input reaches the loop (the wiring this round completed)

- **REPL**: `_ReplReader` (a daemon thread) owns stdin for the whole
  session; `_handle_live` consults the `_LIVE_RUN` registration and
  routes live lines to `steer_live_run` (which appends to
  `logs/{task_id}/steering.jsonl` — the journal the loop's
  SteeringBuffer polls). `steer_live_run` builds its OWN buffer
  instance on the same dir — cross-instance transport via the journal
  file (the harness's `refresh()` re-scan), NOT shared memory.
- **Registration (the previously-missing wiring)**: `_execute_task`
  (fixes + resumes) and `_run_one_build` (builds) call
  `_set_live_run(task_id, log_root)` before the blocking run and
  `_clear_live_run()` in `finally` — without it the reader thought no
  run was live and queued steering into dead air. Both `KeyboardInterrupt`
  and plain exceptions clear the registration (test-pinned).
- **`/cancel` mid-run** raises KeyboardInterrupt in the MAIN thread via
  `inject_async_interrupt` (PyThreadState_SetAsyncExc; ident checked
  against a live thread) — the KI path keeps checkpoints; steering
  abort is the softer, task-level alternative (resumable the same way).

### Acks + labels + help

- `steer_live_run` acks honestly per intent ("steered (seq) — applies at
  the next checkpoint", "steered (re-plan)", "abort requested") and
  refuses when the pending cap is hit, steering is disabled in the
  merged config, or the FIX LOOP is not live yet ("the run is still
  starting" — a trace.jsonl gate: steering typed during a build's
  stage-1 acceptance-test authoring, before run_task's _fresh_paths
  archives any pre-loop journal, would be silently lost otherwise; a
  RESUMED run keeps its prior trace so it is steerable immediately).
  `say` hook routes acks to the TUI transcript.
- `_EVENT_LABELS` gained steering kinds: `steering`,
  `steering_abort`, `steering_replan`, `steering_step_yield` — the
  live monitor/run-line shows steering as it happens.
- `_HELP` documents `/steer` + the plain-text-steers hint; the TUI's
  help test pins its presence.

### Verification at close

- tests/test_steering.py (CLI-surface classes): `steer_live_run` guide/
  abort/convo-refusal/`say`-hook + the pre-loop live-gate (honest
  "still starting" refusal) + resumed-run-steerable + `_execute_task`
  registration & crash-clear (33 non-Docker + 8 Docker-gated e2e;
  41/41).
- tests/test_cli_tui.py: in-flight plain text STEERS (journal write +
  ack, not queued), `/steer` in help, live-run registration mirrored in
  the test backend + cleared in `clean_hooks` (a KI-killed worker never
  leaks a registration) — 33/33.

### Honest notes / known limits

- Steering a task whose run JUST started (before `_set_live_run`, e.g.
  during the build mode's acceptance-test authoring) is answered
  honestly ("the run is still starting — send it again in a moment")
  by steer_live_run's trace.jsonl live-gate; the pre-loop window is
  never silently injected (the journal would be archived with the dir
  by _fresh_paths when the loop starts).
- The REPL's reader thread answers conversational lines inline from the
  reader thread — single console, no cross-thread rendering issues.

## Live agent-trace feed round (2026-09-14) — live reasoning/tool/diff view in the TUI

*(Builds on the full-screen TUI round. Surfaces EXISTING trace/state
data live in the TUI — no new logging path anywhere (Task E below is a
design rule, not an afterthought). New module `cli/tracelog.py`; TUI
wiring in `cli/tui.py`; a REPL-side `/trace` too.)*

### The shape (mirrors Claude Code's visible-thinking texture)

- **NEW `cli/tracelog.py`** — the pure mapping layer: `FeedBuilder`
  folds trace.jsonl events into `FeedEntry` objects (one readable
  one-liner + the FULL raw detail attached per entry), `classify_command`
  maps a bash command to a human action ("Reading src/utils.py",
  "Running: pytest tests/x.py", "Editing mathutil.py" — heredoc rewrites,
  echo-redirects, mv/rm, git status/diff, grep/rg/sed, python -c
  edit-vs-inspect via write_text/open() heuristics, pydoc, curl-fetches),
  `summarize_reply` derives the one-line "visible thinking" from a model
  response (planner -> first planned sub-step; step replies -> first
  prose sentence, command-only replies yield NO line — the tool entry
  that follows IS the action), and `live_diff` computes the inline diff
  from `logs/{task_id}/pristine` vs `logs/{task_id}/work` — the SAME
  trees the harness itself diffs at completion (identical junk-skip set,
  same NUL-byte binary rule, capped, context-2).
- **Tasks A+B in the TUI**: the trace-tail thread (already tailing for
  the run-line) now also feeds every event through the FeedBuilder;
  each produced entry renders into the transcript THE MOMENT it happens
  (verified live: feed lines lag their trace events by <2s, sampled
  during the run). Category glyphs/colors: reason (grey bullet), tool
  (oxide >), diff (oxblood arrow), verify (green check), lifecycle
  (ember).
- **Task C**: after any edit-shaped feed entry (tool_call classified
  edit/write), the TUI renders the real pristine-vs-work diff inline —
  small, colored via the vex.diff.* roles, capped at 14 lines with a
  truncation marker, cp1252-safe box corners via the encoding probe.
- **Task D**: default view is the compact one-liner; every entry
  carries its RAW detail (tool_result output attaches to its command
  entry — expanding shows `$ command` + output, exactly Claude-Code's
  collapsed tool call). `/trace` lists the feed with indexes;
  `/trace <n>` opens `_TraceDetailScreen` — a scrollable modal with the
  full record (400-line cap). Works live AND post-run (post-run
  rebuilds the feed from the run's own trace.jsonl — same data,
  re-derived). The rich REPL gets `/trace` too (plain-print idiom,
  `cli/interactive.py::_trace_feed_command`).
- **Task E (the no-drift rule)**: the feed is a READ-ONLY view over
  `trace.jsonl` — `FeedBuilder` writes nothing (test-pinned), and the
  post-run `/trace` rebuilds from the trace file rather than keeping a
  copy. The harness trace remains the single source of truth; the UI can
  never drift out of sync with the real record.

### Decisions worth knowing

- **`state["feed"]` is the feed's gate, NOT `state["quiet"]`** — the
  TUI worker sets `quiet=True` for every run (it silences the BACKEND's
  LiveMonitor spinner; the run-line replaces it), so gating the feed on
  quiet would have silenced it for every run (found live by the first
  TUI test run). `/quiet` toggles BOTH (spinner verbosity + feed), and
  `log_verbosity = "quiet"` in settings now also starts the feed off.
- **No `step_start` trace event exists** (the label table in
  interactive.py lists one, but the harness never emits it — steps
  surface via `model_request {step: "step-N"}`); step descriptions ride
  the `plan` event, which the builder caches for step-end labels.
- Quiet/silent event kinds by design: `model_request` (raw prompt —
  detail, not a wall), skills/decision-memory misses, empty
  `tool_result`s; malformed events degrade to a skip note, never raise
  (a UI layer must not take a run down).

### Verification (all real, all this round)

- `tests/test_cli_tracelog.py` — **67/67**: classify matrix (25+
  commands), summarize_reply (prose kept, pure-command dropped, SUBMIT
  dropped, fences skipped), live_diff (edit/add/delete/binary/junk-skip/
  truncation caps), FeedBuilder (every event kind, detail attachment,
  malformed-event totality, no-noise kinds), and the no-drift pair
  (deterministic replay; the builder writes nothing).
- `tests/test_cli_tui.py` — **33/33** now (29 prior + 4 new/updated):
  the in-place test now asserts the run-line stays one widget WHILE the
  feed grows exactly one line per action (the phase text itself never
  scrolls); new `TestLiveFeed` (live feed lines render per action with
  detail attached; inline diff after edit with real changed lines +
  diff-colored spans; /quiet suppresses feed but not run-line, entries
  still accumulate for /trace) + `TestTraceCommand` (list + expand +
  post-run rebuild from the trace file + bad-arg handling).
- Full CLI sweep: test_cli_tui 33 + test_cli_tracelog 67 + test_cli 16
  + test_cli_vex 10 + test_cli_vex2 24 + test_cli_vex3 49 +
  test_cli_errors 13 + test_cli_adversarial 62 = **274**; plus
  test_cli_config + test_cli_plugins = 72 parallel-session suites green
  (347 total).
- **LIVE e2e (the round's "before you finish" gate)** —
  `logs/live-feed-e2e/drive_live_feed.py`: the REAL `VexApp` through
  textual's Pilot, typing the bug sentence for bug02_mean, running the
  REAL `harness.core.run_task` (real snapshot/baseline/retrieval/planner/
  step-session/SUBMIT/edit-validation/final-verify/git/rationale) +
  REAL Docker sandbox/verify + REAL inline diff trees; scripted model
  (the documented `set_call_model` hook). **23/23 checks green**:
  feed lines sampled DURING the run (not post-hoc), planner/step/tool/
  verify/lifecycle lines all present, inline diff shows both the old
  (`len(values) - 1`) and fixed (`len(values)`) divisor lines,
  /trace lists + expands with the raw command and its output, the
  per-line lag measured < 2s against the trace file's own write
  timestamps, and the feed's tool entries match exactly the 3 commands
  the model ran. Report: `logs/live-feed-e2e/live_feed_report.json`.
  **Honesty fix (terminal-recovery audit, same day): the first run of
  this gate had a VACUOUS lag check** — its watcher keyed timestamps
  by event kind but looked them up by feed fragment, so zero samples
  ever matched and the "< 2s" claim passed on nothing. The driver was
  rewritten to measure lag for real (first transcript appearance
  sampled in-flight at 0.25s, minus the trace event's own `ts`; a
  missing sample FAILS, never a vacuous pass) and to add the
  during-run /trace check its docstring had promised but never ran.
  Re-run with real Docker: **26/26 green, measured lags 0.17–0.36s**
  across all 5 targets (3 tool lines + planner + plan). The current
  report + lag_samples in `logs/live-feed-e2e/live_feed_report.json`
  are from the fixed driver.
- ruff: cli/tracelog.py + tests/test_cli_tracelog.py + cli/tui.py
  violation-free; interactive.py at its PRE-EXISTING baseline (same
  17-rule set before/after my edits — verified by diffing ruff output
  against the pre-round backup, no new debt). The e2e driver
  (logs/live-feed-e2e/drive_live_feed.py) is violation-free too.

## Plugins & Skills round (2026-09-13) — custom commands (Task B) + plugin bundles (Task C)

*(Tasks A/C harness halves live in harness/skills.py + cli/plugins' tool
hook — see harness/AGENTS.md for the skills scan. This section covers the
CLI-owned surfaces. Both features were on the deferred/stretch list and
had never been built. Modeled directly on Claude Code's real structure:
commands as reusable markdown templates, plugins as bundles of skills +
commands + optional tool/MCP extensions.)*

### Task B — custom commands (`cli/commands.py` + interactive dispatch)

- **Format/locations**: `.vex/commands/<name>.md` (project — committed,
  shared) or `~/.config/vex/commands/<name>.md` (global — personal) or
  `~/.config/vex/plugins/*/commands/<name>.md` (from plugins). No
  frontmatter contract — the file IS the instruction template (dead
  simple by design); `$ARGUMENTS` is the one substitution slot.
- **Invocation**: `/review the auth module` in the interactive session
  → the template's `$ARGUMENTS` is filled, the filled instruction is
  echoed (the user sees exactly what will run), and it runs as a fix
  request (`_run_one_fix`) — a custom command IS a reusable
  instruction for the harness, which is exactly what an issue text is.
- **Shadowing**: built-in slash commands (`/help /status /diff
  /sessions /resume /approve /reject /cancel /quiet`) can NEVER be
  shadowed — `load_command` returns None for those names (pinned by
  test). Precedence on collision: project > global > plugin.
- **Unknown `/x`** now hints the AVAILABLE custom commands (not just
  /help), and `/help` documents the custom-command form.
- Session state carries `repo` + `file_config` (state dict) so custom
  commands run fixes with the same config-file defaults + repo as
  plain-language fixes.

### Task C — plugin bundles (`cli/plugins.py` + `vex plugin` subcommands)

- **A plugin is a directory**: an EXPLICIT `plugin.json` manifest
  (`name`, optional `description`/`version`/`skills` (SKILL.md dirs)/
  `commands` (.md files)/`tools`: {"verbs": [...]}/`mcp_servers`:
  {label: launch command}), or an IMPLICIT layout (no manifest: everything
  under `skills/` + `commands/`, dir name = plugin name) — authoring is
  trivial, explicit manifests unlock tool/MCP extensions.
- **Installed** at `~/.config/vex/plugins/<name>/` by COPY (self-
  contained, removable — never a symlink/reference to the source).
  `vex plugin install <source>` dispatches: local dir path OR git URL
  (depth-1 clone to a temp dir, then the local path; clone failure →
  git's own message, exit 2). Re-install REPLACES (upgrade path).
  `vex plugin list` (name, description, counts of skills/commands,
  tool verbs, MCP refs — plus an honest on-disk recount; a broken
  install lists with its error, never a traceback). `vex plugin remove
  <name>` (name charset-guarded — the remove path can never rmtree
  outside the plugins root; pinned). All PluginErrors → clean message
  + exit 2, per the module's error contract.
- **Tool extensions** (`tools.verbs`): extend the harness BATCH
  read-only allowlist via `harness.tools.extend_batch_verbs` (applied
  at `vex fix` + interactive session start via `apply_tool_extensions`;
  best-effort). The verb deny-token list + composition guard live
  harness-side (see harness/AGENTS.md) — a hostile manifest can widen
  WHICH commands batch, never HOW commands compose.
- **MCP server references** (`mcp_servers`): recorded + surfaced by
  install/list; consumption goes through the EXISTING `vex mcp
  list-tools/call` — a plugin points at servers, it never becomes one.
  **Registry/marketplace explicitly out of scope** per the original
  stretch-list decision.
- **Exit-code contract preserved**: 0 ok / 1 task failure / 2 usage
  error (plugin errors are usage errors, exit 2).

### The example plugin (demonstrates ALL three pieces)

`tests/fixtures/plugin-webapp-toolkit/` — explicit manifest + a
`django-style` SKILL.md + a `/review` command template (review-then-fix
workflow with `$ARGUMENTS`) + `ruff`/`ruff check`/`ruff --version` BATCH
verbs + a `structure-memory` MCP server reference pointing at our own
mcp_server. `vex plugin install tests/fixtures/plugin-webapp-toolkit`
installs it; the skills/commands become discoverable by the harness/CLI
scans immediately (no registration step — that's the plugin discovery
contract: install = drop the bundle in the root).

### Verification (all green, real paths)

- tests/test_cli_plugins.py (30): command loading/precedence/built-in
  shadowing/$ARGUMENTS, interactive dispatch (echo + run-as-fix via a
  monkeypatched _run_one_fix, incl. no-arguments form), plugin manifest
  validation (implicit + explicit + every bad shape), install-local →
  skill/command DISCOVERY roundtrip (installed skills surface in
  harness.skills.discover with origin "plugin"), replace-on-reinstall,
  remove + unsafe-name rejection, git dispatch + clone-failure
  containment, tool-verb extension + the hostile-verb deny list (rm/sed/
  python/curl/git fed as verbs: none validate, `validate_batch` still
  rejects them), and the CLI roundtrip (`vex plugin install/list/remove`
  incl. empty-list + both error paths).
- Live git-URL install proof: a seeded bare repo installed from its
  git URL end-to-end (real clone, real validate, real install).
- Live plugin-chain e2e (Temp/opencode/plugin_e2e.py): install → tool
  verbs applied → real fix task on a django-ish fixture → the plugin's
  skill body demonstrably in the planner's user message + `skills`
  trace event (plugin-origin match) → verified loop SUCCESS.
- Full eval matrix 14×8 = 112/112 CLEAN (the pre-ship gate; the planner
  prompt changed). Regression sweep incl. cli/cli_vex/cli_vex2/
  cli_errors/cli_adversarial: 515 green this round.
- ruff: cli/commands.py + cli/plugins.py violation-free; no new debt on
  any touched file (interactive.py/main.py counts unchanged vs baseline).

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

## Interactive-mode verification round (2026-09-12) — `vex` with no args: GENUINELY WORKING, live-verified; one real CRASH found + fixed

*(Verification round for Task D of the Vex CLI pass. The prompt: "cd
/path/to/any/repo; vex" must drop into an interactive session, accept a
typed sentence, and start working — Claude Code / Codex style.)*

### The verification

Drove the REAL installed `vex.exe` (not `python -m cli`) in a scratch
copy of the smoke_repo fixture, dispatching through the EXACT
condition in `cli/main.py main()` (no args + `sys.stdin.isatty()`;
the driver installs a sitecustomize hook that scripts the model and
serves the typed lines through a fake-TTY stdin — same code path a
human at a terminal hits). Result: **12/12 checks green** — banner +
repo line + prompt glyph rendered, the typed sentence
("mean() in mathutil.py returns the sum; make it the mean") accepted
as the issue text, the REAL harness loop ran (27 trace events, 5
model calls, Docker-sandboxed verify), SUCCESS + attempts/cost line
+ verification PASS chips + colored diff + markdown rationale
rendered, `bye` on exit, session recorded in
`logs/.vex-sessions.jsonl` with status success, original repo
byte-identical (never-mutate holds), rc=0. Driver + report kept at
`Temp/opencode/vex-interactive-check/` (drive_interactive.py,
interactive_report.json, transcript in stdout_tail).

### The real bug the live drive found (and why every test missed it)

**`cd <repo>; vex` used to CRASH with RecursionError before the first
model call.** The interactive session defaults `log_root` to `./logs`
UNDER the CWD — which IS the target repo in this flow. The harness
snapshots the repo into `logs/{task_id}/pristine`; dst inside src made
`shutil.copytree` descend into its own destination and recurse until
RecursionError (task status "error"). Every scripted caller — all CLI
tests, benchmarks, OSS runs — placed logs OUTSIDE the repo, so the
shape was simply never exercised; the fake-TTY drive hit it in 20
seconds.

**Fix (harness/editor.py::snapshot — a harness-module edit, flagged
here per cross-terminal practice):** when dst's parent chain runs
through src, snapshot excludes the top chain segment (e.g. `logs`)
from the copy — correct regardless of where the log root came from
(interactive default, `--log-root`, `HARNESS_LOGS_DIR`), and the
pristine reference shouldn't contain harness artifacts anyway.
Regression-pinned BOTH ways in tests/test_editor_prompts.py (4 new
tests, 19/19 file green): log-root-inside-repo copies repo content but
not the log chain; log-root-OUTSIDE-repo still copies a same-named
`logs/` dir that is real repo content (no over-exclusion); dst
directly under src edge; plus the plain non-recursive assertion.
Post-fix re-drive: 12/12 checks green, same task flow end to end.

### Regression sweep after the editor.py fix (all green, 340 tests)

test_editor_prompts 19 (incl. the 4 new), test_e2e_run_task 28,
test_cli 16 + test_cli_vex 10 + test_cli_vex2 24 + test_cli_adversarial
62 + test_cli_errors 13 (125), test_adversarial 43 +
test_coordination 31 + test_config_trace_state 12 (86), test_stubs_
and_deps + test_retrieval_tools + test_recall_unit (51),
test_coordination_e2e + test_decision_memory_planning +
test_env_snapshot (32). One NOT-OURS failure fixed in passing:
`harness/ablation_agenttests.py` (a parallel session's in-flight
untracked file) carried a PowerShell UTF-8 BOM that tripped the
module-parse guard; stripped the 3 BOM bytes, content untouched —
12/12 that file after.

### Test command for the project owner (the Task B deliverable)

```
cd /path/to/any/repo
vex
```
Expect: the Vex banner (amber/ember theme, `cli/ui.py`'s VEX_THEME —
the stand-in documented in DESIGN-v1-backup.md; no literal
`VEX_DESIGN_SYSTEM.md` file exists or ever did), a `vex ›` prompt,
and typing a plain sentence starts the loop with live spinner →
SUCCESS/FAIL + verification chips + diff + rationale. `help` lists
session commands; `exit`/Ctrl+D quits; non-TTY no-args still prints
argparse usage + exit 2 (tested; CI-safe).

### Status: NOT scaffolded — genuinely working

The dispatch (`argv is None and not raw and sys.stdin.isatty()`), the
session loop, live monitoring, session persistence, and now the
cd-any-repo log-layout shape are all live-verified through the real
console script. The one missing piece found (the in-repo log root
crash) is fixed and pinned.

## PyPI packaging round (2026-09-12) — distribution name `vex-harness`, built + clean-venv-verified, publish left to the owner

*(Task A-D of the "Real pip install via PyPI" prompt. Name availability
checked LIVE against PyPI's JSON API, not assumed: `vex` (unrelated
legacy pkg, v0.0.19), `vex-cli` (an AI CLI that itself installs a `vex`
command — direct conflict, owner scivor.ai), `vexx`, and `pyvex` are
all TAKEN. Available short candidates found: `vexcli`, `vexfix`,
`vexai`, `vexe`, `vex-harness`, `vex-code`, `vex-agent-cli`. Owner
chose **`vex-harness`** from the shortlist.)*

### What shipped

- **pyproject.toml**: `[project] name = "vex-harness"` (console scripts
  unchanged — `vex` primary, `harness` legacy alias), plus the
  PyPI-page metadata that was missing: `readme`, `authors`,
  `[project.urls]` (Homepage/Repository/Issues/Changelog), `keywords`,
  `classifiers` (3.10-3.12, Beta, Console, Bug Tracking/QA).
- **README.md**: install docs now lead with `pip install vex-harness`
  then `vex`, subcommand block uses `vex ...` (was `harness ...`), with
  a note explaining the name/command split (beautifulsoup4→bs4
  analogy) and that clone-based flows (`harness ...`, `python -m cli`)
  keep working.
- **dist/**: `vex_harness-0.1.0-py3-none-any.whl` (352 KB) +
  `vex_harness-0.1.0.tar.gz` (451 KB, 145 files — source packages
  only; no logs/, demo-work, or fixtures swept in; stale `vex.egg-info`
  removed). Wheel METADATA verified complete (readme rendered as
  Description-Content-Type: text/markdown).

### Clean-venv verification (fresh venv, wheel only, no repo on path)

All 8 top-level packages import; `vex --help` shows every subcommand
(fix / run-benchmark / status / memory / dashboard / mcp); `vex
--version` → `vex 0.1.0+source`; `pip show vex-harness` correct.
Also smoke-drove `vex fix` on the smoke_repo fixture: reaches the
planner and fails ONLY on missing API credentials (expected without a
key — honest error, no crash), full trace.jsonl written.

### NOT done (deliberately — needs the project owner)

`twine upload` requires the owner's PyPI account + API token — NOT
attempted. When ready: `python -m pip install --upgrade twine` then
`python -m twine upload dist/*` (upload BOTH the wheel and the sdist).
First upload creates https://pypi.org/project/vex-harness/.

## Branding round (2026-09-13) — oxblood theme, block wordmark, splash vs. compact header, and the intent-gate BUG FIX

*(Tasks A-F of the "Vex CLI — Branding, Splash/Header, and a Real Bug
Fix" prompt. Package confirmed `vex-harness`, command `vex`.)*

### Task E FIRST — the real defect (bug fix, not a feature)

**Typing `hi` in an interactive session launched
`fixing in coding-harness (task fix-c52ff16b)` — ANY input was treated
as a bug report.** Root cause: `run_interactive` fell straight into
`_run_one_fix(line, ...)` for every non-command line; nothing ever
asked whether the line *was* an issue description.

**Fix: NEW `cli/intent.py`** — a deterministic, offline, no-model
classifier (`classify(line) -> Intent{kind, reply}`) with three
outcomes, wired into the session loop BEFORE `_run_one_fix`:

- **convo** — greetings (`hi/hello/hey...`), thanks, meta questions
  about vex itself ("what can you do", "who are you", "what model"),
  plain chit-chat shapes ("how's it going") → answered inline, NO task
  launched.
- **fix** — bug language (crash/fails/returns wrong/off-by-one/...)
  and/or source artifacts (`foo()`, `tests/x.py::t`, `src/`) and/or
  fix verbs with an object ("fix the login bug") → the real harness
  loop, exactly as before.
- **ambiguous** — everything else (bare questions, "help me move
  apartments") → ONE clarifying question, never a silent task launch.
  The cost asymmetry drove this: a wrong run burns minutes + model
  budget; a question costs one line.

Slash commands and bare session commands (`repo/model/help/exit`)
are dispatched before the gate and never classified. Custom commands
(`/name`, commands.py) also bypass the gate — they are explicit fix
requests by construction.

**Regression-pinned**: `tests/test_cli_vex3.py::TestIntentGate` — 21
conversational inputs (incl. the original repro `hi`, `what can you
do`, `help me`) must NEVER launch; 10 real bug sentences (incl. the
canonical mean() sentence) MUST launch; 4 ambiguous lines must ask;
plus two WIRING tests driving the real session loop with monkeypatched
input: `hi` answered with `_run_one_fix` monkeypatched to explode if
called; the bug sentence DOES reach `_run_one_fix` with the sentence
as issue text.

### Task A — the wordmark (oxblood gradient, ANSI-shadow letterforms)

The old serif figlet banner was illegible; the first rebuild's flat
`█` block rows read "1990s BBS" (owner feedback, 2026-09-13 second
pass). Final design in `cli/ui.py`:

- **ANSI-shadow box-drawing letterforms** (`██╗ ██╗███████╗...`, the
  figlet style OpenCode/Claude Code-class CLIs use), 6 rows,
  uniform 26-col span, **horizontal oxblood→oxide→warm gradient per
  column** (`gradient_text` + `_VEX_RAMP #8C2B2E→#C9504C→#D98E5F→
  #F0C9A8` — hand-rolled; rich 14.3 has no Gradient class).
  Probe-gated: `╔` crashes cp1252 consoles → '#' block fallback
  (flat accent color), same GLYPS discipline. Distinctive
  letterform fragments pinned in tests (stronger than geometry:
  a bad edit can't quietly produce an illegible mark).
- **Color: oxblood** — the referenced `VEX_DESIGN_SYSTEM.md` does not
  exist (third round hitting this). The prompt asked for "the design
  system's violet accent"; the project owner was asked and chose
  **oxblood** instead. Measured (contrast on #000000, not guessed):
  true oxblood #4A0E0E–#7A1F23 is 1.4–2.1:1 — ILLEGIBLE on a
  terminal (the old amber was 6.9:1). Owner-selected ramp:
  **accent #C9504C (~4.7:1)**, **running/oxide #D98E5F (~6.6:1)**.
  Theme roles otherwise unchanged (ok=green3 semantic, error=red3,
  warn, muted, diff.*). test_cli_vex.py's no-color-strips test holds.

### The modern-surface grammar (owner-requested redesign, same day)

One visual language across every surface (rich roles: grey58 labels,
grey70 values, accent2 emphasis, `·` dot separators — cp1252-safe):

- **Splash**: gradient wordmark, tagline ("... · verified, not vibed",
  dot in oxblood, verdict in green — the honesty line IS the design),
  grey35 hairline Rule, aligned `label value` info rows, hint row.
- **Compact header**: `◆ vex 0.1.0 · model <m> · <repo>` one line.
- **Prompt**: `vex ›` (wordmark-accent name, grey glyph).
- **Run line**: `→ fixing in <repo> · ⏱ <task_id>`.
- **Spinner**: oxide label + `· N events · $cost` ticker.
- **Result**: `✔ SUCCESS · 1 attempt · 5 model calls · 42s · $0.0021`
  then `PASS target · PASS regression · flaky: no` chips, then
  `run N events · N model calls · N tokens · $cost` status line.
- **Rules** for section transitions (rich Rule, hairline style).

### Task C — two-tier display: splash once, compact header after

- **Splash** (`ui.print_splash`): gradient wordmark + tagline +
  hairline + repo/logs/model/version rows + hint line. Shown when
  `_is_first_launch(log_root)` — no session index AND no run dirs
  with traces under the log root (harness artifact dirs like
  `_code-graph` correctly DON'T count; pinned).
- **Compact header** (`ui.print_compact_header`): ONE line —
  `◆ vex 0.1.0 · model <m> · <repo>` (◆ = the ember mark, Task B)
  plus a logs/hint line. Every regular session start (Claude Code
  per-session pattern). The old `_BANNER` constant is retained one
  release (documented) but no longer printed.

### Task B — mascot: the single ember mark ◆ (decision + rationale)

A multi-row pixel-art creature was prototyped mentally against the
26-col wordmark and rejected: at header scale it read as noise, and
the brief's own bar was "skip it rather than ship something
mediocre." Shipped instead: the **single ◆ ember glyph** as the vex
sigil in the compact header (ASCII `*` fallback via GLYPS) —
Claude-Code-mascot-scale, not Claude-Code-mascot-art. Documented
here so a future session can revisit deliberately, not silently.

### Task D — loading animation: live-verified ACTIVE (not just built)

Drove the REAL `vex` console script (fake-TTY stdin hook + scripted
model + REAL harness + REAL Docker sandbox/verify) with
FORCE_COLOR=1 on a pipe so rich's Status actually emits frames:
**165 spinner frames**, live phase labels cycling ("model: thinking
(agent-tests-1)", "step done", "writing rationale", "finishing"),
running cost ticker, then the summary line. Driver + 3-session
report (30/30 checks): `Temp/opencode/vex-branding-check/`
(drive_branding.py, branding_report.json).

**Bonus real defect found while verifying: `spinner="dots"` was a
latent cp1252 crash.** rich's braille frames UnicodeEncodeError on
legacy Windows consoles when ANSI is forced (probe-reproduced; same
bug class as the original GLYPS fix). Fix: `ui.SPINNER` —
encoding-probed (`"dots"` if braille encodes, else ASCII `"line"`),
used by `ui.status()` AND `LiveMonitor.start()` (both had
`spinner="dots"` hardcoded). Pinned in test_cli_vex3.

Verification methodology note: on Windows pipes, rich's
legacy-windows color system drops ANSI color codes entirely
(verified by probe — `colorsys windows`, no `\x1b[`) while FORCE_COLOR
still makes Status render frames as \r-overwritten text; the drive
asserts frames + labels + cost from the \r stream, which is the
product behavior on a real terminal too (spinner + label text).

### Task F — parity pass

- Prompt: `vex ›` with wordmark-colored `vex` (accent) + `›`
  (accent2) — consistent with header/model labels.
- LiveMonitor summary line reshaped to a proper status line:
  `run events: N | model calls: N | tokens: N | cost: $X` (cost in
  accent2, matching the result line's cost emphasis).
- Compact header + status line + result chips now share one visual
  grammar (◆/● marks, label-colon fields, accent2 values).

### Files this round

| File | Change |
|---|---|
| `cli/intent.py` | NEW — the Task E classifier |
| `cli/ui.py` | oxblood theme; wordmark/splash/compact-header; `SPINNER`; ember glyph |
| `cli/interactive.py` | intent gate in the loop; splash/compact split (`_is_first_launch`, `_print_session_head`, `_session_model_label`); SPINNER in LiveMonitor; summary-line reshape; `state` kwarg on `_execute_task` (quiet + future session context) |
| `tests/test_cli_vex3.py` | NEW — 49 tests: theme/wordmark/splash/header, first-launch detection, the Task E intent matrix + loop wiring, spinner safety, status line |
| `tests/test_cli_vex2.py` | 2 `fake_execute` stubs gained the new `state` kwarg (same intent) |

### Verification

- tests/test_cli_vex3.py: **49/49**
- Full CLI sweep: test_cli 16 + test_cli_vex 10 + test_cli_vex2 24 +
  test_cli_vex3 49 + test_cli_errors 13 = **113/113** (one run)
- test_cli_adversarial: **62/62** (Round-6 hardening intact)
- Parallel-session suites sharing these modules: test_cli_config +
  test_cli_plugins = **71/71**
- Live drives: 30/30 checks over 3 real sessions (Task C/E/D above)

## Two-tier config + custom router round (2026-09-13) — global+project settings, ex config, base_url support

*(Supersedes the single-tier ~/.vex/config.toml design from Vex Side
Task 2's Task C — that file still WORKS via the legacy fallback.)*

### Task A — the two-tier directory structure (Claude Code's pattern)

`
Global   %APPDATA%\vex\settings.toml   (Windows)
         ~/.config/vex/settings.toml   (POSIX, XDG_CONFIG_HOME honored)
          wins when set (test isolation / portable installs)
Project  <repo>/.vex/settings.toml          committable, no secrets
         <repo>/.vex/settings.local.toml    personal overrides, AUTO-added
                                             to the repo's .gitignore when
                                             Vex creates/touches it
Legacy   ~/.vex/config.toml                read ONLY while the new global
                                             file is missing (migration)
`

- Project tier is found by walking up from the CWD for a .vex/ dir
  ($VEX_PROJECT_DIR points AT the .vex dir; authoritative even before
  it exists on disk, so config set --tier project can create it).
- **Precedence (highest→low): explicit flags/session > env (VEX_MODEL,
  VEX_PROVIDER, VEX_BASE_URL, VEX_API_BASE, VEX_API_KEY) >
  project-local > project > global > legacy > built-in defaults.**
  Every level conflict-tested (tests/test_cli_config.py:TestPrecedence).
- Created ONLY by Vex itself: first-run flow (interactive startup calls
  ensure_first_run → creates the global file + one-time notice) and
  ex config set/init-project. install.sh/pip never write it.
- ex config subcommands: path / list (effective values + source
  tier per key + chain files + un-ignored-local warning) / get KEY /
  set KEY VALUE [--tier global|project|local] / unset KEY [--tier] /
  init-project. set appends in place (hand-written comments survive),
  rewrites only when updating an existing key; refuses to touch a file
  it can't parse. api_key is masked in list/get (sk-…… (set)), never
  printed back.
- .gitignore handling is AUTOMATIC and idempotent: set --tier
  local|project and init-project append the ignore entry when missing
  (exact/whole-dir/glob coverage detection); .gitignore is CREATED only
  inside a real git repo; list warns when a local file isn't covered.
- TOML BOM tolerance: _read_settings reads utf-8-sig — Windows
  editors (PowerShell/Notepad) write BOMs that silently disabled a whole
  tier before the fix (found live in the e2e; same class as the subset
  loader's utf-8-sig fix).

### Task B — custom router / base URL (any OpenAI-compatible backend)

- New settings keys: ase_url (user-facing name) and pi_key /
  model as independent values — settable via env
  (VEX_BASE_URL/VEX_API_KEY/VEX_MODEL/VEX_PROVIDER/VEX_API_BASE)
  or any settings file. ex fix --base-url added as an alias of
  --api-base.
- 
ormalize_runtime_keys() (cli/vexconfig.py) maps ase_url onto
  runtime's existing pi_base context key and defaults provider to
  "openai" (litellm then dials {base_url}/chat/completions with
  whatever model name the router serves — NOT a fixed provider list).
  Explicit provider/api_base always win. Applied in _make_task
  (flag commands) AND _run_one_fix (interactive) so both paths get it.
- **REAL e2e, no model flags** (logs/config-e2e/): ex fix with
  ase_url = https://api.tokenrouter.com/v1 + model z-ai/glm-5.3-free
  from the settings FILE + VEX_API_KEY env → real router, verified
  fix (target+regression PASS, minimal divisor diff), 3 calls,
  `.0058`, 58s, EXIT 0. Env-only route (VEX_BASE_URL+VEX_MODEL,
  no file) also proven: 12 calls, 43k tokens, target+regression PASS
  (wall-clock timeout in the free endpoint's slow window AFTER verify
  passed — endpoint load, not config; the file route run above is the
  clean success record). Config-passthrough keys exercised in the same
  file: max_wallclock_s, self_critique, gent_tests.

### Gotchas found live this round

- A parallel terminal's stray .vex/settings.local.toml (model = "m")
  in the REPO ROOT walked up into the e2e's project tier and hijacked
  the model — the precedence machinery working as designed; e2e reran
  with VEX_PROJECT_DIR isolation. Lesson encoded: verify effective
  settings (ex config list) before blaming connectivity.
- PowerShell Set-Content writes UTF-16/BOM — it corrupted the e2e
  settings file; the broken-TOML guard caught it (clean warning,
  defaults applied, no crash), then the BOM fix made the tier readable.

### Files this round

| File | Change |
|---|---|
| cli/vexconfig.py | rewritten: two-tier chain, 4-level precedence, env tier, legacy fallback, tier writers (append-preserving), ex config backend, gitignore automation, 
ormalize_runtime_keys |
| cli/main.py | ex config subcommand tree; _make_task now merges the settings chain + normalizes base_url; --base-url alias |
| cli/interactive.py | first-run flow (creates global settings, one-time notice); session config from merged_settings(); _run_one_fix normalizes runtime keys |
| 	ests/test_cli_config.py | NEW — 42: tier paths, full precedence conflicts at every level, base_url (file/env/flags), config subcommands, gitignore handling, BOM tolerance, first-run, subprocess smoke |
| .gitignore | .vex/settings.local.toml entry (added automatically by the feature itself, kept for the repo root) |

### Verification

- tests/test_cli_config.py **42/42**; full CLI sweep across all 8
  suites (incl. the parallel sessions' plugins/branding/config ones):
  **247/247**; ruff clean on all touched files (main.py/interactive.py
  remain at their pre-existing lint-baseline counts, no new violations);
  real e2e above.

## CLI citizenship round (2026-09-14) — release readiness: --help/--version polish, exit-code categories, completions, self-update, uninstall, --json

*(The "Release Readiness — CLI Citizenship" prompt: Tasks A-G, the
basic behaviors every professional CLI has. All additive; the
historic 0/1/2 exit-code semantics and every existing flag/output are
byte-identical except the two NEW split categories.)*

### Task B FIRST — exit codes (the one contract change)

- **NEW `cli/exit_codes.py`** — the single numeric source of truth:
  `EXIT_CODES = {success 0, task_failure 1, usage_error 2,
  environment_error 3, model_error 4, interrupted 130}` +
  `reason_for()` + `classify_exit_code(exc)` (never raises). The 0/1/2
  semantics are UNCHANGED; 3 (Docker/sandbox/deps) and 4 (litellm/
  endpoint/auth/rate-limit) split what the catch-all 1 used to
  swallow, so CI can treat "fix the machine" differently from "the
  bug beat the agent". Classification maps the SAME layers
  `cli.errors._classify` documents — `cli.errors` gained
  `failure_category()` and `explain_exception` now prints
  `category: <name> (exit code N)` next to the diagnosis.
- **Wired at three points**: `cmd_fix`'s harness-crash path,
  `cmd_run_benchmark`'s exception path, and main()'s BaseException
  safety net — all now `return classify_exit_code(exc)`. The safety
  net's old comment ("exit code 1, not 2: the usage was valid") kept
  its intent: unmapped exceptions still classify to 1.
- **Pinned**: test_cli_release.py::TestExitCodes (the table, usage=2,
  task=1 via a real offline fix run, env=3 via exploding run_task
  raising SandboxUnavailableError, model=4 via
  litellm.APIConnectionError, never-raises on a hostile exception) +
  test_cli_errors.py updated: the Docker-crash pin moved 1 -> 3 (the
  round's only intentionally-changed assertion; documented in the test
  docstring) and the unmapped-exception pin stays 1.

### Task A — --help / --version

- `--help`: epilog documents the exit-code contract;
  `RawDescriptionHelpFormatter` (the literal-`\n` rendering issue);
  usage metavar lists PUBLIC commands only (hidden `__completions`
  stays dispatchable via `sub.choices` but invisible — on 3.10/3.11
  argparse renders SUPPRESSed subparsers as `==SUPPRESS==`, so
  `add_completion_parser` filters `sub._choices_actions`, the help-only
  registry; tested on 3.10.11 live).
- `--version`: already existed (install round); now PINNED to
  pyproject.toml by test (installed dist OR the `+source` fallback).

### Task C — NO_COLOR / --no-color

Already honored (rich + the main() hooks). Now test-pinned end to end:
env var and late subcommand-level flag both strip every ANSI code
from real command output (test_cli_release.py::TestColorControl).

### Task D — shell completion (NEW cli/completion.py)

- `vex completion bash|zsh|fish|powershell` prints the script;
  `--install` writes it to the conventional location: bash ->
  `$XDG_DATA_HOME/bash-completion/completions/vex`, zsh ->
  oh-my-zsh/completions else `~/.zfunc/_vex`, fish ->
  `vendor_completions.d/vex.fish`, PowerShell -> APPENDS to
  `$PROFILE` (idempotent via the marker line — the first
  implementation had marker != body first-line and double-appended;
  caught by the idempotence test, fixed).
- **Dynamic, not frozen**: the scripts shell out to the hidden
  `vex __completions` backend (REMAINDER-parsed words, one candidate
  per line, trailing `--` sentinel); the backend walks the REAL
  argparse parser DEEPEST-subcommand-first (`config set --tier <TAB>`
  completes the choices — the naive first-match version completed
  against config's parser, not set's; fixed by walking words through
  nested registries). Completions can't drift from --help. Hidden from
  help listings + `_visible_subcommands` filters `__*`.
- Backend invocation uses `sys.executable -m cli` (portable across
  pip/pipx/venv/source installs; the generated scripts embed the
  absolute interpreter path).

### Task E — vex update (NEW cli/selfupdate.py)

- `detect_install_method()`: pipx (path contains pipx+venvs) /
  the installers' `~/.vex-venv` / pip / source-checkout (parent of
  cli/ has .git). venv+pip -> `python -m pip install --upgrade
  git+https://...@main` (mirrors installers; VEX_INSTALL_SOURCE/
  _REPO/_REF overridable); pipx -> `pipx upgrade vex-harness` with a
  reinstall fallback; **source -> honest refusal** printing the
  git pull + `pip install -e .` recipe, exit 1 (this machine IS a
  source checkout — verified live).
- `--check` compares installed vs latest: GitHub tags API first,
  **PyPI JSON API fallback** (found live: THIS environment's GitHub
  API returns 404 for the repo — the tags exist per `git ls-remote`,
  and PyPI answers; the dual-source design was validated by exactly
  the filtering it exists for). Unreachable -> exit 4 (network
  category). `vex update` proper shows the command it runs; failures
  map 1 (upgrade failed) / 3 (can't launch the toolchain).

### Task F — vex uninstall (NEW cli/uninstall.py)

- `collect_plan()` enumerates ONLY Vex-created things, probing the
  same layout functions the installers/settings use (no blind
  rmtree): pipx venv (noted, removed via pipx's own command),
  `~/.vex-venv`, `~/.vex/bin` shims, the Windows user-PATH entry
  (registry API, same as install.ps1 — never setx), config roots via
  `cli.vexconfig.global_settings_path/legacy_settings_path`.
- Shows the plan, confirms (or `--yes` / `--dry-run`), removes,
  prints the pip/pipx one-liner for the package itself. "nothing
  Vex-created found" is a clean exit 0 with the source-checkout hint.
  README carries the full manual recipe too.

### Task G — --json output

- `vex fix --json`: spinner/LiveMonitor quiet, no theme lines on
  stdout; ONE JSON document (`_result_json`: task_id, status,
  attempts, cost, model_calls, elapsed, diff, log_path,
  verification{target/regression/flaky}, exit_code, exit_reason).
  Exit codes unchanged by --json (0/1 by outcome; 2/3/4 by category
  on crash paths). **REGRESSION FOUND LIVE + FIXED**: litellm's error
  banner prints to STDOUT — a real no-credentials run polluted the
  JSON document. In --json mode the run's stdout is diverted to
  stderr (`sys.stdout = sys.stderr` around run_task) so the document
  stays the ONLY thing on stdout; the banner then lands on stderr
  where it belongs. Pinned by
  test_json_diverts_foreign_stdout_printers.
- `vex status --json`: `_load_status_state` shared by both renderers
  (human + JSON can't disagree about the facts); progress/plan/
  files/decisions/result-cost in one document; missing/invalid ->
  exit 2 with the error on stderr.
- Verified through the REAL offline fix e2e (scripted model, real
  run_task, real Docker verify): success payload parses, failed-run
  payload carries exit_code 1 + task_failure reason, stdout has zero
  `[vex.` markup (pinned).

### Files this round

| File | Change |
|---|---|
| cli/exit_codes.py | NEW — the numeric contract + classifiers |
| cli/completion.py | NEW — 4 shell scripts + dynamic backend + install |
| cli/selfupdate.py | NEW — update/--check, install-method detection, GitHub+PyPI version sources |
| cli/uninstall.py | NEW — plan/confirm/remove incl. PATH registry edit |
| cli/main.py | epilog + metavar, --json on fix/status, category wiring at 3 exit points, 3 new subcommands, _result_json/_load_status_state |
| cli/errors.py | failure_category() + the category line in explain_exception |
| tests/test_cli_release.py | NEW — 49 tests across all 7 tasks |
| tests/test_cli_errors.py | Docker-crash pin 1->3 (intent documented); unmapped stays 1 |
| README.md | Exit-codes table, --json, completion, update/uninstall sections |
| CHANGELOG.md / INTERFACES.md | round entry + Change Log entry (exit-code contract) |

### Verification

- tests/test_cli_release.py: **50/50**
- Full CLI sweep, one run: test_cli 16 + test_cli_vex 10 +
  test_cli_vex2 24 + test_cli_vex3 49 + test_cli_errors 13 +
  test_cli_release 50 = **162/162**; second run: test_cli_adversarial
  62 + test_cli_config 42 + test_cli_plugins 30 + test_cli_tui 20 =
  **154/154** (sweep incl. the parallel sessions' suites sharing these
  modules — all still pass against this round's changes).
- Live drives: `vex --help` (all 10 public commands + exit-code
  epilog, no hidden machinery), `vex --version` (0.1.0, matches
  pyproject + CHANGELOG), `vex update --check` (live PyPI fallback
  answered "up to date 0.1.0" in the GitHub-filtered environment),
  `vex update` (source-checkout refusal, exit 1), `vex uninstall
  --dry-run` (found the real config root, removed nothing),
  `vex completion bash/zsh/fish/powershell` (all four generate),
  `vex __completions` end-to-end incl. flag-shaped partials.
- ruff: all NEW files violation-free; main.py held AT its
  pre-existing lint-baseline count (10 — the one new I001 I
  introduced was fixed); interactive.py's lint debt is a PARALLEL
  session's in-flight TUI work, untouched by this round (flagged in
  the ratchet output like the standing practice).

### Not yet implemented / honest notes

- The completion scripts use the ABSOLUTE interpreter path captured
  at generation time (portable, but a moved/removed Python
  installation orphans them — regenerate with `vex completion
  <shell>` after major Python upgrades). A `vex`-on-PATH invocation
  would tie them to the console script instead; deferred because
  `vex` may be a shim whose interpreter is exactly this one anyway.
- GitHub-API filtering was observed live in THIS environment (404 on
  api.github.com with the repo reachable over git+https and pypi.org
  answering) — the dual-source version check is the mitigation, not
  an assumption about GitHub.

## Real full-screen TUI round (2026-09-14) — textual App REPLACES the rich-print loop; wordmark everywhere; thinking orbit + tech jokes

*(The "Vex — Real Full-Screen TUI" prompt: Tasks A-D, plus the owner's
follow-up asks — the VEX wordmark visible on every session, a distinct
thinking symbol, and rotating technical jokes while the agent thinks
(Qwen-Code style). This REPLACES the rich-print interactive loop as
the primary UX; the REPL itself remains as the non-TTY/VEX_TUI=0
fallback, unchanged.)*

### What shipped

- **NEW `cli/tui.py`** — a persistent full-screen `textual` App
  (`VexApp`), layout per the prompt's diagram: compact header
  (◆ vex · version · model · repo, live status chip on the right),
  scrollable transcript (`RichLog`, rich markup, themed), a run-line
  widget (THE live status during a run — updated in place, never
  scrolled past), the input box, and an OpenCode-style hint bar
  (/help · /status · /diff · ctrl+c · ctrl+p · ctrl+q). CSS applies
  the Vex palette: oxblood #C9504C accents, oxide #D98E5F
  focus/running, dark #0d0d10/#15151a surfaces — deliberate, not a
  1980s print-out.
- **Task B (the core difference from the REPL)**: a run blocks its
  WORKER thread, never the UI. cli.interactive fires the new
  `_ON_TASK_START(task_id)` hook the moment the task id exists; the
  app spins a trace-tail thread that folds `trace.jsonl` events into a
  `_RunState` and repaints the SAME run-line widget per event (+ a
  0.125s UI timer for the animation frames). Nothing re-prints.
- **Thinking treatment (owner ask, Qwen-Code-style)**: during
  `model_request` → `model_response` the run-line switches to a
  distinct ORBIT glyph (◐◓◑◒ — `ui.THINK_FRAMES`, ASCII fallback)
  and shows a rotating TECHNICAL joke (`ui.JOKES`, 18 all-tech
  one-liners, all-ASCII, ~4.5s cadence via `ui.joke_at`). The same
  treatment lands in the rich REPL's LiveMonitor so `vex fix` and the
  fallback REPL match. Jokes live in cli/ui.py — one table, both
  surfaces.
- **Wordmark on EVERY session (owner ask)**: `_print_splash` now runs
  on every app mount — the gradient oxblood wordmark + tagline are the
  shell's face; first launch adds the full info rows (repo/logs/
  model/version), later sessions keep a lean hint row. No more
  brandless blank-screen session starts.
- **Task C — nothing lost**: every built-in slash command (/status
  /diff /sessions /resume /approve /reject /cancel /quiet /help),
  bare commands (repo/model/exit/help), the intent gate (hi never
  launches a run), custom commands, /cancel semantics, plan preview
  and the approval gate — all inside the shell. Plan preview +
  approval prompts become MODAL screens (`_PromptScreen` /
  `_ConfirmScreen`) with the plan steps / diff as the modal body;
  ctrl+p opens an OpenCode-style command palette (built-ins + custom
  commands, filter + arrows + enter).
- **Backend reuse, not reimplementation**: the TUI renders
  cli.interactive's backend. Backend output is captured via
  `_CapturedConsole` (rich segments recorded, replayed into the
  transcript as styled Text — spans keep their resolved colors);
  `_m()` rewrites `[vex.*]` markup roles to concrete textual colors
  from `ui.VEX_THEME` (one palette source). The backend's blocking
  `input()`/`print()` are patched during a run so they become
  modals/transcript lines (time-scoped patch: the backend's helper
  threads also prompt).
- **/cancel + Ctrl+C in the TUI**: the REPL raised SIGINT at its main
  thread; here the run lives in a worker, so the `_CANCEL_RUN` hook
  injects an async KeyboardInterrupt via ctypes
  `PyThreadState_SetAsyncExc` (ident-checked against the live worker
  — idents get recycled; seen live). run_task's KI path then stops
  containers, keeps checkpoints, records the resumable session.
- **Dispatch + fallback**: `cli.main` no-args + TTY → `run_tui()`
  (`can_run_tui()`: textual importable, stdout a TTY, VEX_TUI≠0);
  anything else → the rich REPL, never a crash. Non-TTY no-args
  still prints argparse usage + exit 2 (CI-safe).

### The round's real defect (found + fixed, honest note)

**cli/tui.py was left by the interrupted session with TRIPLE-encoded
mojibake** — every `·`/`—`/`◆`/braille glyph had been round-tripped
utf8→cp1252 three times (63+ corrupted sequences; the file even
carried C1 control bytes). The TUI would have rendered garbage
separators on real terminals. Fixed by full rewrite from the intact
contract (the module's own docstrings + the passing 21-test suite
were the spec); every glyph is now real UTF-8 (probe-verified: 23 ·,
52 —, ◆/→/⠋ present, zero C1 controls, AST clean).

### Two MORE real bugs found while verifying (both fixed, both pinned live)

1. **Cross-suite stdout swallowing (the `_CapturedConsole` file-restore
   leak)**: `_cap.capture` snapshotted rich's `con.file` GETTER — which
   resolves dynamically to sys.stdout, i.e. textual's `_PrintCapture`
   while an app runs — and restored it BY ASSIGNMENT, freezing that
   app-lifetime object as the shared console's permanent file. Every
   later print in the process vanished (repro: any TUI modal test
   followed by a REPL test → `assert '/status' in ''`). Fix: save/
   restore the EXPLICIT `_file`/`_width` state (None stays None — the
   console goes back to following sys.stdout). Repro script kept at
   Temp/opencode/repro_leak.py; the pre-fix probe showed
   `console.file = textual.app._PrintCapture` after app exit, post-fix
   real stdout.
2. **Frozen `color_system="windows"`**: rich resolves the color system
   ONCE at Console construction from the THEN-current
   sys.stdout.isatty(); if the shared (lru_cached) console's first
   construction happened inside a textual run, the poisoned value
   stuck for the whole process (ANSI codes leaking into piped output
   forever after). Fix: EAGER `console()` at cli.ui import (before
   any app/test can swap the streams) + `set_no_color` now also
   `console.cache_clear()` so the flag still applies to a rebuilt
   console (the eager build made a stale no_color possible otherwise).

### Parallel-session note (2026-09-14, this round's close)

A second live session is building the live action feed (`cli/tui_live.py`
FeedBuilder + `/trace` command + TestLiveFeed/TestTraceCommand suites)
ON TOP of this round's tui.py — the file at close carries BOTH
(their 4 in-flight test failures are theirs, mid-edit; this round's 27
+ the REPL/adversarial sweeps are green against the combined tree,
182/182 with the in-flight classes deselected). Coordinated via this
note per the cross-terminal practice.

### Files this round

| File | Change |
|---|---|
| cli/tui.py | NEW — the textual App + modals + palette + capture/role-map + hooks (mojibake repaired, then the round's upgrades) |
| cli/ui.py | + JOKES / joke_at() / THINK_FRAMES (shared thinking treatment) |
| cli/interactive.py | + _ON_TASK_START / _CANCEL_RUN / _PROMPT_BODY hooks (embedded-UI contract); LiveMonitor thinking state + joke in the spinner label; escape import |
| cli/main.py | no-args TTY dispatch → run_tui (REPL stays the fallback) |
| pyproject.toml | + textual>=0.40 |
| tests/test_cli_tui.py | NEW — 27 tests through textual's real Pilot harness |
| Temp/opencode/drive_tui_visual.py | live visual drive (11/11 checks) |

### Verification

- tests/test_cli_tui.py: this round's classes **27/27** (real app,
  real event loop, real modal screens — order-sensitive per the file's
  docstring; run with `-p no:randomly` if pytest-randomly is
  installed). The parallel session's TestLiveFeed/TestTraceCommand
  were mid-flight at close (the note above).
- Combined-tree sweep (TUI classes + REPL suites in ONE process — the
  contamination canary): test_cli 16 + test_cli_vex 10 +
  test_cli_vex2 24 + test_cli_vex3 49 + test_cli_errors 13 +
  test_cli_config 42 = **182/182** after the two fixes above (was
  14-failed before them).
- test_cli_adversarial + test_cli_plugins: **92/92** (Round-6
  hardening intact).
- Live visual drive (headless Pilot, scripted backend): **11/11** —
  wordmark rows + tagline on open, orbit glyph + tech joke +
  `$0.0031` cost ticker in the run-line during model_request,
  in-place updates (transcript line count stable across 12 frames),
  status chip running→idle, run-line hidden post-run.
- ruff: cli/tui.py + cli/ui.py violation-free; interactive.py's
  remaining findings are the pre-existing baseline (os/Task forward
  ref — none from this round's added lines).

### Not yet implemented / honest notes

- **Three-OS test**: only Windows (Windows Terminal + the Pilot
  headless runs) was live-tested this round. textual's Windows driver
  enables VT processing itself; POSIX/macOS are expected to behave
  (textual's cross-platform surface) but NOT verified here — flagged
  for the next live pass.
- The capture-based transcript replays backend output when the run
  FINISHES (not streaming line-by-line); live per-event visibility is
  the run-line's job (by design — nothing scrolls during a run).
- `_prompt_patches` is time-scoped (global during a run) by
  documented necessity: the backend's helper threads (plan-preview
  watcher) also prompt. One run at a time is enforced by the app's
  queueing; a hypothetical concurrent second run would misroute
  prompts (never happens from the UI).

## Live todo + status panel + completion card round (2026-09-15) — structured live views in the TUI sidebar

*(The "Vex TUI — Live Todo List, Status Panel & Completion Summary"
prompt: Tasks A-C, all reading data the harness ALREADY tracks — the
plan/state machine/cost ledger — never a parallel tracking system. This
is the structured companion to the free-form live trace feed: that
round made actions visible; this round makes the SHAPE of the run
visible.)*

### What shipped

- **NEW `cli/runview.py`** — a pure READ-ONLY view layer over the
  run's own on-disk records (same discipline as cli/tracelog Task E:
  it writes nothing, ever, and every public function is total —
  malformed events/missing files degrade to honest empties):
  - `TodoModel` folds the harness's OWN events into a live checklist:
    `plan` builds the list, `model_request {step-N}` marks the ACTIVE
    step, `step_end.ok` checks it off (✘ on failure),
    `step_skipped_resume` checks off pre-crash-completed steps,
    `attempt_start > 1` unchecks everything (the harness rolls work
    back and resets completed steps on a retry — the trace replays
    survivors), and a steering REPLAN keeps checkmarks for steps whose
    description already completed (the re-plan BUILDS ON work/, it
    never undoes it).
  - `read_machine_state` reads `transitions.jsonl` (the state
    machine's documented audit trail) for the last valid to-state —
    with a fix over `state_machine.current_phase`'s blind spot:
    steering rounds append `{event: ...}` records with NO to_state
    (deliberately keeping the phase unchanged), and a "first field
    found" reader would surface the string "None"; this reader takes
    the last record that actually CARRIES a to_state.
  - `read_run_facts` + `card_lines` re-derive the completion card's
    numbers from the run's own records at render time (status/
    attempts/cost from the `result` event — falling back to usage-sum
    + task_end when a crash prevented one; model_calls counted from
    the trace; elapsed from the trace's own ts span; files from
    state.json; branch/commit from `git_output`) — because the facts
    are re-read, the card can never drift from what actually happened.
    Question/research get the read-only variant (no files/tests/
    branch rows).
- **TUI sidebar (Task B, the layout)**: the app's middle row is now
  transcript + a 34-column sidebar (`#vex-side`): the live todo
  checklist (Task A — ○ pending / ▸ active / ✔ done / ↷ skipped-resume
  / ✘ failed, cp1252-safe fallbacks via `ui._enc_ok`, descriptions
  clipped at 30 chars, capped at 14 rows) and the status panel (Task
  B): mode · state-machine state · elapsed · running cost. The sidebar
  opens with the run (the same `_ON_TASK_START` attach), ticks with
  the existing 0.125s UI timer (elapsed/cost advance between events),
  re-reads `transitions.jsonl` on lifecycle-ish events (refreshed
  BEFORE the repaint so a batch shows the transition it carries), and
  collapses 2s after the run's final snapshot.
- **Mode routing (prerequisite this round exposed)**: the TUI
  previously funneled EVERY non-convo line into a fix (it gated with
  `cli.intent`, whose fix-vs-convo view can't see the other three
  modes — a BUILD request would have run the fix engine). The TUI now
  dispatches through `harness.router.route_kind` — the SAME two-tier
  classifier the rich REPL uses — so fix/question/build/research all
  run from the TUI with per-mode workers (`_fix_worker` aliases the
  original `_run_worker`; `_mode_worker` is the shared body — each
  mode's backend has its OWN signature, research takes repo last, so
  the caller binds a zero-arg closure).
- **`_fire_task_start` for question/research** (cli/interactive.py):
  both backends pre-generate their task id and fire the embedded-UI
  hook BEFORE the blocking model call (the same contract
  _execute_task/_run_one_build honor) — without it the TUI's
  run-line/sidebar never attached for those modes. REPL behavior is
  unchanged (hook unset there; firing is a no-op).
- **Completion card (Task C)**: at `_finish_run` the tail thread does
  a FINAL DRAIN (joined, bounded ~2s) so the closing events land
  before the final todo snapshot — without it the sidebar could show a
  stale mid-run checklist next to a finished card — then one boxed
  summary renders into the transcript: status/mode/issue head,
  attempts · model calls · elapsed · cost chips, files, target/suite
  PASS-FAIL verdicts, branch+commit. The backend's raw result scroll
  still replays (the capture); the card is the polished curation ON
  TOP of it.

### Files this round

| File | Change |
|---|---|
| cli/runview.py | NEW — TodoModel, read_machine_state, read_run_facts, card_lines, fmt_elapsed (pure view layer, writes nothing) |
| cli/tui.py | sidebar layout (CSS + compose), _RunState.todo/mode, _render_side, _refresh_machine_state, _render_card, _teardown_side, mode dispatch (_start_run(mode)/per-mode workers), _tail_trace final drain, _tick_spinner sidebar tick |
| cli/interactive.py | _run_one_question/_run_one_research pre-generate ids + fire _ON_TASK_START |
| tests/test_cli_runview.py | NEW — 35 unit tests (todo folding incl. retry/replan/skip semantics, machine-state reader incl. steering records, facts derivation, card shapes) |
| tests/test_cli_tui.py | TestTodoSidebar (4) + TestModeDispatch (3): live checkmarks, panel fields, card-vs-records, collapse; build/question dispatch; convo still never launches |
| logs/todo-status-e2e/ | the round's real-task e2e gate (below) |

### Verification

- tests/test_cli_runview.py: **35/35**. tests/test_cli_tui.py:
  **40/40** (this round's 7 + the prior 33 — order-sensitive per the
  file docstring, `-p no:randomly`).
- Full CLI-module sweep, one process: test_cli 16 +
  test_cli_adversarial 62 + test_cli_config 42 + test_cli_errors 13 +
  test_cli_plugins 30 + test_cli_release 50 + test_cli_runview 35 +
  test_cli_tracelog 22 + test_cli_tui 40 + test_cli_vex 10 +
  test_cli_vex2 24 + test_cli_vex3 49 + test_modes 26 +
  test_state_machine 42 = **554/554** (one first-run flake of the
  documented worker-teardown class re-ran clean twice; the pre-Docker
  outage also produced 2 unrelated test_cli failures — daemon down,
  both pass with it up).
- **The round's gate — REAL multi-step task through REAL Docker**
  (logs/todo-status-e2e/drive_todo_status.py, same scripted-model
  pattern as the live-feed e2e): **16/16**, including the two the
  prompt demands: (1) the todo checklist updates at the ACTUAL moment
  each step completes — measured against the trace events' own `ts`
  (plan listed 0.34s, step-1 checkmark 0.13s, both << the 2s bound);
  (2) the card's numbers match the records EXACTLY (status, attempts,
  cost to the 6th decimal, model-call count vs the trace, files vs
  state.json, branch/commit vs git_output, panel states a subset of
  transitions.jsonl's trail). Report: logs/todo-status-e2e/
  todo_status_report.json.
- Question mode probed live through the TUI (scripted model): the
  sidebar attaches via the pre-generated id, the answer renders, and
  the card shows the read-only variant (`SUCCESS · question`, calls/
  time/cost/trace rows — probe transcript kept).

### Not yet implemented / honest notes

- The todo's ACTIVE marker comes from `model_request {step-N}` — the
  first event inside a step session. A step whose session starts
  before the plan is re-emitted (steering replan mid-attempt) can show
  the previous active briefly; harmless (the next request corrects
  it), noted for completeness.
- The card shows branch + commit, not a hosted PR URL — the harness's
  git-native output produces a branch/commit/PR-description on the
  local repo, and there is no forge in the loop; the card shows
  exactly what exists. If a forge integration lands, `card_lines`
  gains one row from the same facts dict.
- `attempt(s)` keeps its parenthetical form (1 attempt(s)) — "1
  attempt" / "2 attempts" pluralization is applied to model calls
  only; keeping attempts unambiguous pluralization-free was a
  deliberate choice for the width-stable chip row.
- Sidebar width is fixed at 34 cells (min 26); narrow terminals
  (< ~100 cols) squeeze the transcript rather than hiding the
  sidebar. An auto-collapse threshold for tiny terminals is future
  polish (the CSS `display: none` toggle already exists for the
  collapsed state).


## Design-system compliance + hero layout round (2026-09-15) — locked oxblood palette applied; two real crash/layout bugs fixed

*(The "Vex TUI — Design System Compliance & Layout Fix" prompt: Tasks
A-D. A compliance pass, not a redesign — VEX_DESIGN_SYSTEM.md already
specified everything. An earlier interrupted session had already
re-tokenized the shared theme (cli/ui.py — see its module docstring:
the oxide-orange `vex.running` was removed, active = oxblood) and part
of the TUI; this round found the residue, fixed two real bugs the
tests had never exercised, and closed the palette out.)*

### Task A — locked palette (what was actually left)

- `cli/tui.py` still carried non-token colors in three places: the
  modal/hint CSS (`#5f5f5f` random grey x4), the palette list
  (`#D98E5F` oxide labels + `#767676` hints), and rich-name greys
  (`grey58`/`grey70`) in the run banner, completion card, copy-diff
  borders, live-diff context lines, and queued-run line. All replaced
  with tokens: `text-secondary` (#A89490) for muted/hint text,
  `accent` (#C9504C) for selected/active, `text-primary` (#F0E8E5)
  for values, `text-secondary` for diff context. `#5f5f5f` and
  `#767676` are gone from the TUI entirely.
- `cli/interactive.py` (the rich REPL fallback) still used `grey58`/
  `grey70`/`#767676` throughout — same palette contract (the design
  doc's "CLI (rich Theme object)" surface), so they were mapped to
  `vex.muted` / `text-primary` / `text-secondary`. Styling-only; the
  full REPL/adversarial suites stayed green.
- `_VEX_RAMP` (the wordmark gradient) is the LOCKED logo — untouched,
  as instructed. The logo itself is #C9504C->#F0C9A8 warm; the audit
  explicitly allows its gradient interpolation stops.

### Task B — true near-black background (two real framework leaks)

The Screen background was already pinned to `bg-base` in CSS, but the
**rendered SVG proved two framework defaults were still leaking**
(SVG export + programmatic css-variable dump, not eyeballing):

1. **Scrollbars**: textual's default theme ships a pure **BLACK**
   track (`scrollbar-background = #000000`, `scrollbar-corner-color =
   #000000`) and a primary-derived dark-red thumb (`#50201E`) — 24+
   black cells ran down the transcript edge in the export. The old
   `scrollbar-color: A B` shorthand was a no-op for the track. Fixed
   by pinning the scrollbar theme variables to tokens
   (`scrollbar-background` = bg-base, `scrollbar` = border-subtle,
   hover/active = accent).
2. **Focused-input tint**: textual's `Input:focus` default is
   `background-tint: $foreground 5%`, which renders **#241c1c** (a
   non-token lighter blend). The focused input now explicitly sets
   `background: $bg-panel-hover` (`#241717`, the token for elevated/
   interactive surfaces) with `background-tint: ... 0%`.
3. **Doc vs prompt**: the prompt text said `bg-base` is `#0A0A0F`, but
   VEX_DESIGN_SYSTEM.md (the authoritative file, re-read as
   instructed) says `#0A0808` — a warm near-black. Implemented
   `#0A0808`; the rendered background is exactly that.

### Task C — hero layout (a crash + a stacking bug)

The hero info block had **never actually rendered** on first launch:
`_print_splash` copied rich `Table.rows` via `row.cells`, and rich's
`Row` has no public `cells` attribute on this version ->
`AttributeError` in `on_mount`, which cascaded into ~30 test failures
across the file (the first-launch path was simply broken). Repaired
by carrying the info as plain `(key, Text)` data.

The layout was ALSO wrong even if it hadn't crashed: rich `Columns`
auto-stacks its items once the pair exceeds the console width (wide
Windows temp paths made it stack on any realistic terminal), which
reproduces the exact "stack of printed lines" look Task C removes.
Replaced with a **manual two-column row assembly** — one `Text` per
visual row: gradient logo row + gap + right-aligned key + value —
vertically centered against the 6-row wordmark, with long values
left-truncated to the remaining width so the hero can never overflow
(or silently stack). Pinned by `test_hero_info_sits_beside_logo_not_below`.

### Task D — empty space

Already addressed by the prior round's sidebar (the live todo +
status panel fill the right rail during a run; the prompt explicitly
conditions on those existing). The transcript's border is the only
divider above the input (no empty band); nothing further needed.

### Verification (the round's "before you finish" gate)

- **SVG export + token audit** — `Temp/opencode/drive_tui_compliance.py`
  drives the REAL `VexApp` through textual's Pilot (first-launch hero,
  a live scripted run, and the finished state), exports all three
  screens, and classifies EVERY color in the SVG against
  VEX_DESIGN_SYSTEM.md's token list. Result: **3/3 screens COMPLIANT,
  zero unexplained colors** (report: `Temp/opencode/tui_compliance_report.json`;
  SVGs: `vex_tui_hero.svg`, `vex_tui_live.svg`, `vex_tui_done.svg`).
  Remaining non-token hexes are exactly two sanctioned sets: the
  locked logo gradient's interpolation stops, and textual's own
  screenshot window chrome (frame/title/traffic-light dots).
- tests/test_cli_tui.py: **40 -> 43** (TestDesignSystem gained the
  scrollbar/background-tint pin, the hero two-column layout pin, and
  the modal-token pin; the stale oxide/orange pins were corrected).
  Full CLI sweep (12 suites, one process): **440/440**.
- tests/test_cli_vex3.py: the branding-round oxblood pin updated
  (`vex.running` is now the logo oxblood, not oxide orange — the
  compliance round's documented change) and re-asserted `vex.glow` =
  accent-glow.
- ruff: `cli/tui.py`, `cli/ui.py`, both touched test files
  violation-free; `cli/interactive.py` held at its pre-existing
  working-tree baseline (the 15 findings are imports/nesting/
  placeholder-less f-strings from earlier rounds — string-only color
  edits here changed no placeholder status and added zero).

### Honest notes

- The screenshot "chrome" (#292929 frame, #c5c8c6 title, #ff5f57/
  #febc2e/#28c840 dots) is drawn by `App.export_screenshot()`, not the
  app; it is excluded from the audit by design and documented as such.
- The focused-input background is now `bg-panel-hover` (a token) but it
  is a deliberate semantic choice (focus = elevated/interactive
  surface); if a designer wants the input to stay exactly `bg-panel`
  while focused, drop the `background:`/`background-tint:` lines from
  `#vex-input:focus`.

## Interaction-polish round (2026-09-16) — fuzzy palette, syntax-highlighted diffs, scrollback/searchable history, live multi-task dashboard, reasoning/action distinction, completion notification

*(The "Vex TUI — Advanced Interaction Polish" prompt: Tasks A-F,
navigation/interaction upgrades on top of the design-compliance /
layout / live-visualization rounds. One new module, edits across
tui/ui/interactive/main/runview; every change is CLI-side, no
cross-terminal contract changed.)*

### Task A — real command palette (`cli/fuzzy.py` NEW + `tui.py`)

- **NEW `cli/fuzzy.py`** — a dependency-free, UI-agnostic subsequence
  matcher (VS Code/fzf feel): `fuzzy_score(query, text)` (prefix /
  word-boundary / camelCase / contiguity bonuses, substring fast-path,
  length penalties; `None` when the query isn't a subsequence),
  `rank(items, query)`, and `filter_and_rank(records, query, key)`
  (a record also matches on its `hint`, deboosted, so "resumable"
  finds a session whose LABEL is just its task id). Multi-word queries
  AND. Total on any input (non-strings str()'d — the palette feeds it
  raw typing).
- **The palette is now one search, not a menu** (`_PaletteScreen`):
  built-in commands + custom commands + recent sessions + REPO FILES,
  fuzzy-ranked, in a real scrollable `OptionList` (mouse + arrows/
  page/home/end while the filter box keeps focus — screen-level
  priority-free `on_key` forwards nav). Typing re-ranks live; results
  cap at `_MAX_RESULTS` for the display while the scan runs over the
  whole set.
- Sources: `_palette_entries()` (commands → sessions → files, so the
  curated no-query view leads with commands); sessions ride
  `list_sessions(limit=60)`, files ride **`scan_repo_files(repo)`** —
  `git ls-files` first (validating each path resolves under `repo`, so
  an ancestor repo can't leak the wrong tree), bounded cache-skipping
  `os.walk` fallback, cap 4000, `.vex`/logs/node_modules/cache dirs
  skipped, cached per repo on the app.
- Selection semantics (`_palette_chosen`): a no-arg command RUNS
  immediately (the palette executes — /help //sessions //feed //status
  //diff //diff), an arg-taking command (/resume /steer /trace) and a
  FILE prefill the input (a file path is the start of a sentence, not
  a command); a session prefills/runs its exact `/resume <id>`.

### Task B — syntax-highlighted diffs (`ui.py`, shared)

- **`ui.diff_render_lines(diff)` / `ui.diff_text(diff)`** — the ONE
  language-aware diff renderer. `_DiffHighlighter` tracks the current
  file from `+++ b/<name>`, picks a pygments lexer
  (`guess_lexer_for_filename` → extension map → None), and renders each
  line as a rich Text: the `+`/`-`/`@@` role color under pygments token
  colors (theme "ansi_dark"). Unknown language / lexer crash / binary /
  truncation markers fall back to the flat +/-/@@ roles — the diff's
  OWN information never depends on the syntax layer. Accepts a diff
  string, a list of line strings, OR the `(text, kind)` tuples
  `cli.tracelog.live_diff` returns (it re-derives kind from the prefix,
  so every surface colors identically).
- Applied to the TUI inline preview, the TUI `/diff`, the approval
  modal diff body (`_diff_body`), `ui.print_diff` (the REPL + `vex
  fix`), and the REPL `/diff`. Colors are concrete token hexes (the
  Text objects render under textual's RichLog, which has no rich
  theme). The compliance SVG audit's "no unexplained colors" rule still
  holds: these are rich/pygments renderable colors inside spans, the
  same category the earlier round carved out, NOT CSS chrome.

### Task C — scrollable + searchable history (`tui.py`, `interactive.py`)

- **Transcript scrollback** (`_TranscriptLog`): the live feed's
  auto-follow PAUSES the moment the user scrolls away from the bottom
  and RESUMES when they return. Hooked on textual's single low-level
  `_scroll_to` (every scroll — mouse wheel, page keys, the shift+↑/↓/
  pageup/pagedown/end bindings added to the app — funnels through it);
  a `was_following and at_bottom` test distinguishes RichLog's own
  auto-follow write from a user scroll.
- **`/feed [query]`** opens `_FeedBrowserScreen` — the whole run's trace
  feed as a scrollable, SEARCHABLE list (filter over summary + category
  + detail), reasoning styled apart from actions, enter expands an
  entry in the trace-detail modal ON TOP (the browser stays as the
  history). `/feed` works live and post-run (rebuilds from the run's
  own trace.jsonl — the shared `_collect_feed_run()` now drives
  `/trace` and `/feed`, no second copy kept, Task E no-drift intact).
- **`/sessions` is a searchable browser** (`_SessionsScreen`) rather
  than a flat print, and shares one pure filter grammar with the REPL:
  **`cli.interactive.session_matches`/`filter_sessions`/
  `search_sessions`** — tokens `status:`/`repo:`/`task:`/`day:`/
  `since:`/`until:` (ISO date bounds), bare `resumable`, free-text
  AND-matched over task_id/issue/repo/status; an unevaluable filter
  narrows to nothing (honest, never "all"). `/sessions <query>` in the
  REPL prints the filtered list; in the TUI it opens the browser
  pre-filled. `search_sessions` scans 4x the limit so filtering a long
  history finds the needle, not just the 15 newest.

### Task D — live multi-task benchmark dashboard (`main.py`, `runview.py`)

- **`_BenchmarkLiveView` is a real dashboard now**: one rich Table row
  per concurrent task — status · phase · elapsed · model calls · cost ·
  tier · model. Refactored so the render is testable: `rows(now=)`
  (pure) + `render(now=)` (Table) + a thin `_loop`.
- **NEW `cli.runview.read_task_progress(log_root, task_id)`** — the
  read-only per-task fold (same discipline as `read_run_facts`: writes
  nothing, total on half-written files). trace.jsonl → status/phase/
  model_calls/cost/tokens/models + the first-event ts; the runtime's
  `model_ledger.jsonl` (Boundary 2's per-call ledger) → the difficulty/
  routed hint that picks the model tier + the models seen. Queued / no-
  trace tasks get an honest zero record so the whole set shows at once.
- **The live set** comes from the real scheduler's `live_attempts()`
  (`getattr(run, "__self__", None)` — `Scheduler.run` is a bound method
  whose `__self__` has `live_attempts`; its `_Attempt.started_epoch`
  drives a running task's wall-clock elapsed even before its trace
  exists — a queued row under a live attempt is corrected to running).
  The stub scheduler has no live view, so the table degrades to
  trace-file facts + a running count — never a crash. `note_result`
  still lands final statuses; the dead `_feed_live_view` helper is gone.

### Task E — reasoning vs action in the feed (`tui.py`, `interactive.py`)

- Reasoning lines are now **italic + text-secondary** (dimmed, the
  agent THINKING); tool/diff/action lines are **bold accent oxblood**
  (upright, the agent DOING) — scannable at a glance without reading
  each line. Moved to module-level `FEED_GLYPHS`/`FEED_STYLES`/
  `feed_style`/`feed_line`/`feed_line_text` (ONE style table for the
  live transcript, `/trace`, and the `/feed` browser — VexApp now
  delegates, no drifting copies). The REPL `/feed`/`/trace`
  (`interactive._print_feed`/`_feed_style`) carries the same
  italic-vs-bold distinction so both surfaces agree. verify lines stay
  outcome-aware (success green / error red only on the real verdict).

### Task F — completion notification (`ui.bell`, `interactive`, `tui`, `main`)

- **`ui.bell()`**: the terminal bell (`\a`) + a non-blocking
  `ctypes.windll.user32.MessageBeep` on Windows (some terminals swallow
  `\a`). TTY-ONLY (a `\a` byte into a pipe is literal garbage — a
  piped `vex fix`/`--json` stays byte-clean) and silenced by
  `VEX_NOTIFY=0` (CI / ssh / audio-free).
- **One ring per finished task**: the TUI rings at `VexApp._finish_run`
  (covers all four modes); the REPL rings via
  `cli.interactive.notify_done(status)` at the end of
  `_execute_task`/`_run_one_question`/`_run_one_build`/
  `_run_one_research` — and `notify_done` DEFERS to the TUI whenever the
  embedded-UI `_ON_TASK_START` hook is mounted (the TUI reuses
  `_execute_task`, so without the guard a TUI fix would double-beep).
  Flag commands (`vex fix`, `vex run-benchmark`) ring from `main.py`.

### Files this round

| File | Change |
|---|---|
| cli/fuzzy.py | NEW — dependency-free fuzzy subsequence matcher + ranking |
| cli/tui.py | fuzzy OptionList palette (commands+sessions+files) + `scan_repo_files`; `_TranscriptLog` scrollback; `/feed` browser + `_collect_feed_run`; `_SessionsScreen` browser; `_SearchListScreen` base; shared feed renderers (reasoning/action italics); syntax-highlighted inline diff; bell in `_finish_run` |
| cli/ui.py | `diff_render_lines`/`diff_text` language-aware diff renderer; `bell()` |
| cli/interactive.py | `session_matches`/`filter_sessions`/`search_sessions`; `/feed`+`/diff`+`/sessions` REPL rendering w/ reasoning-action styling; `notify_done` |
| cli/main.py | `_BenchmarkLiveView` → real dashboard (rows/render/_loop + live_attempts); bell on `fix`/`run-benchmark`; dropped dead `_feed_live_view` |
| cli/runview.py | NEW `read_task_progress` (read-only per-task fold for the dashboard) |
| tests/test_cli_polish.py | NEW — 38 tests incl. the realistic-scale + real-repo gates |

### Verification

- tests/test_cli_polish.py: **38/38** — fuzzy matrix; **palette at scale
  (300 sessions + 600 files): scan skips node_modules/__pycache__ +
  git's ancestor-repo guard, entries build <5s, "cltui" ranks
  cli/tui.py first, "fix-029" finds a session, filter <2s**; **the
  real-repo gate opens the palette against the actual coding-harness
  working tree (485 scanned files, real git ls-files) and fuzzy-finds
  cli/tracelog.py through the running app**; selection
  behaviors (run-vs-prefill); syntax-diff (python tokens colored,
  filename→lexer, unknown-language + garbage + `(text,kind)` tuple
  shapes degrade); session filter grammar (status/repo/task/day/since/
  until/resumable/AND free-text + 300-session needle); feed browser
  (filter + enter-expands-over-detail); transcript scrollback
  (auto-follow pauses on scroll-up, a write does not yank back, bottom
  resumes); read_task_progress + dashboard rows (running/queued/done,
  cost authority, tier from ledger, live_attempts wall-clock, stub
  degradation); bell (TTY-only, env-silenced, REPL-defers-to-TUI).
- tests/test_cli_tui.py: **43/43** (the flat `test_inline_diff_after_edit`
  span pin re-targeted to the new language-aware contract: the +/-
  marker carries the diff color AND python keywords are their own
  colored token segments — stronger than before, not weaker; the
  `test_sessions_lists_recorded` test updated for the browser + its
  status:/repo: filters).
- Full CLI sweep (every tests/test_cli*.py, one process): **480/480**;
  test_cli_tui.py alone 43/43 twice. test_steering.py + test_modes.py
  (the interactive seams this round touched): 140, and the
  test_cli*+steering combined run 520 on its clean re-run (the first
  combined run hit the documented order-sensitive
  `test_run_line_updates_in_place` worker-teardown flake once — passes
  in isolation, per the test_cli_tui.py file docstring).
  `python -m evals.run --check`: 12 tasks OK (fixtures still resolve
  through the loop; no prompt changes).
- ruff: `cli/fuzzy.py`, `cli/ui.py`, `cli/tui.py`, `cli/runview.py`,
  `tests/test_cli_polish.py` violation-free; `interactive.py` held AT
  its pre-existing 15-finding working-tree baseline (imports/nesting/
  placeholder-less f-strings from earlier rounds — none from this
  round's new/edited lines; verified by inspecting every flagged line
  is pre-existing); `main.py` new lines clean (the runview import
  sorted to ruff's isort preference; the remaining findings are the
  pre-existing Task/TaskResult forward-ref + scan_mode F401 baseline).

### Honest notes / known limits

- The palette's file list is cached per repo on the app instance for
  the session — a fix that CREATES files mid-session won't appear until
  a new session; deliberate (a 4000-file walk is the cost, not free per
  palette-open). `git ls-files` is capped at 3s then falls to the walk.
- Diff syntax highlighting is PER LINE (pygments can't carry a token
  across hunk lines), so a multi-line string/comment colors from its
  first line; the +/-/@@ role is always exact regardless. Falls back to
  flat diff roles when the language can't be identified — never a
  syntax-layer failure.
- `read_task_progress` cost for a running task is the live usage-sum;
  the `result` event (when present) is the authority and replaces it,
  mirroring the completion-card precedence.
- The bell fires at TUI `_finish_run` and REPL `notify_done` — an
  interrupted (Ctrl+C) run does NOT ring (it wasn't "finished"), only
  completed tasks do.
- No INTERFACES.md contract changed: everything here is CLI-surface
  (the read-only trace/ledger/state files the harness/runtime already
  document are consumed, not extended; `runview.read_task_progress`
  joins the module's existing read-only-view surface).

## Packaging / installer round (2026-09-21) — v0.2.0, PyPI-first installs

*(The "stale pip install" prompt: `pip install vex-harness` and the
one-line installers now land on the same working build.)*

### pyproject.toml

- Version **0.1.0 -> 0.2.0**; `_get_version`'s `+source` fallback
  tracks it (the version-pin test reads pyproject, so both move
  together).
- Missing deps declared: **`pygments>=2.13`** (the language-aware
  diff renderer needs it — a clean install without it hits
  ModuleNotFoundError on first diff render) and
  **`tomli>=2.0; python_version<'3.11'`** (3.10 has no stdlib
  tomllib; without it the whole settings chain silently degrades to
  defaults). Deliberately NOT added: `docker` (the sandbox shells
  the Docker CLI — no SDK import anywhere in the tree, verified),
  `psutil` (dev-only soak tooling, guarded lazy imports with
  degrade paths).
- `litellm==1.74.9` pin kept (newer breaks the `typing` import on
  3.10 — see README "Tech").
- `[tool.setuptools.package-data] cli = ["fixtures/**/*"]` — the
  smoke fixture now ships in the wheel/sdist (before: `vex
  run-benchmark --subset smoke` failed on pip installs with "smoke
  fixture repo missing"). setuptools warns the fixture dirs look
  like importable packages absent from `packages` — benign (they
  have no `__init__.py`; they ship as DATA, verified present in
  both artifacts). `dist/` rebuilt (0.2.0 wheel+sdist, `twine check`
  PASSED); stale 0.1.0 artifacts removed.

### Installers (install.sh/.ps1/.cmd) — PyPI by default

- Default source is now the PyPI spec `vex-harness`
  (`pipx install vex-harness` / `pip install --upgrade vex-harness`);
  `VEX_INSTALL_SOURCE` wins outright, else an explicitly-set
  `VEX_INSTALL_REPO`/`_REF` pins the git URL (all four resolution
  cases probed live under real cmd.exe + Git bash + PS parser).
- Banners read "the AI coding agent for your terminal".
- Preflight: Python 3.10+ hard-fails; git required only for git-URL
  sources (warn-only on the PyPI path); Docker warn-only everywhere.
- Post-install runs `vex --version` + `vex update --check`
  (best-effort, never fails the install). Line-ending contracts
  unchanged and byte-verified (cmd/ps1 CRLF, sh LF; `bash -n` clean).

### cli/selfupdate.py — PyPI-first

- `install_source()` returns `vex-harness` unless a git pin is
  explicit (behavior change, logged in INTERFACES.md Change Log);
  `latest_available_version()` asks PyPI JSON first, GitHub tags as
  fallback. `vex update` for pip/venv methods now upgrades from the
  same source that installed it; source-checkout refusal unchanged.

### Verification

- Clean-venv install of the 0.2.0 wheel (fresh venv, no repo on
  path): `vex --version` -> 0.2.0, all subcommands resolve, smoke
  fixture on disk, `vex update --check` reports against live PyPI.
- tests/test_cli_release.py: 43 passed excluding the Docker-gated
  TestJsonMode trio (daemon down on this machine — runs error with
  attempts 0 before any model call; unrelated to this round).

### Not done (needs the owner)

- `twine upload` — needs the PyPI API token. When ready:
  `python -m twine upload dist/*` (dist/ now holds ONLY the 0.2.0
  pair, so the glob is safe). Until then PyPI serves 0.1.0 and
  `vex update --check` on a 0.2.0 install honestly reports
  "update available" against it.
