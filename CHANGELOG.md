# Changelog

High-level milestones of the build, newest first. This is a readable
summary of what the system can do at each stage — not a commit log.
Per-module detail lives in each module's `AGENTS.md`; the cross-module
contracts in `INTERFACES.md`; the full plan in `project-spec.md`.

## v0.2.1 (2026-09-22)

Ships the whole daily-use CLI surface that the 0.2.0 wheel predated
(built before these files landed): first-run onboarding (`vex login`
wizard incl. TUI modal, `vex logout`, `/model`, exit-4 gate),
`.vex/` repo scaffolding, full-screen TUI, plugin enable/disable +
`vex mcp` registry + `vex skills`, `/plan /review /compact /copy-diff
/history /trace /feed /steer` parity, fuzzy palette,
syntax-highlighted diffs, live benchmark dashboard, `vex
analyze-history`, completions, self-update, and `vex uninstall`.
No model set + `vex` -> inline wizard (once), saves, never asks
again: `vex login` (Official OpenAI/Anthropic/Gemini + Router
OpenRouter/TokenRouter/Ollama/Custom with free-text base_url/model,
live-tested before saving), `vex logout`, `/model`, TUI modal
variant, flag-command gate (missing model exits 4, never prompts),
project files can never hold secrets, settings chmod 600 (POSIX).

## v0.2.0 (2026-09-21)

The daily-driver release: `pip install vex-harness` and the one-line
installers now always land on the same working build.

- **Packaging** — version 0.2.0; the missing runtime deps are declared
  (`pygments` for the language-aware diff renderer, `tomli` for TOML
  settings on Python 3.10; Docker stays a system requirement, not a
  pip dep — the sandbox shells out to the Docker CLI; `psutil` stays
  undeclared — dev-only soak tooling with guarded lazy imports).
  `litellm==1.74.9` pin kept (newer breaks the `typing` import on
  3.10). `cli/fixtures/smoke_repo` now ships inside the wheel/sdist
  (`[tool.setuptools.package-data]`), so `vex run-benchmark --subset
  smoke` works from a pip install instead of failing with "smoke
  fixture repo missing". `vex --version` stays pinned to
  pyproject.toml by test.
- **Installers** (`install.sh` / `.ps1` / `.cmd`) — PyPI-first:
  `pipx install vex-harness` / `pip install vex-harness` by default;
  `VEX_INSTALL_SOURCE` (or an explicitly-set `VEX_INSTALL_REPO` /
  `_REF`) still pins a git checkout. Banners now read "the AI coding
  agent for your terminal". Preflight: Python 3.10+ (hard fail), git
  required only for git-URL sources, Docker warn-only. Post-install:
  `vex --version` + `vex update --check` (best-effort, never fails
  the install). Line-ending contracts unchanged (cmd/ps1 CRLF,
  sh LF — see `.gitattributes`).
- **Self-update** (`vex update`) — `--check` asks PyPI first, GitHub
  tags as fallback (the order the old code had was backwards for a
  PyPI-default world); upgrades run through the same method/source
  the install used; source checkouts still get the honest `git pull`
  refusal.
- `twine upload` of the rebuilt wheel+sdist needs the owner's PyPI
  API token — run `python -m twine upload dist/*` to publish.

## Vex TUI interaction-polish round (2026-09-16)

Navigation/interaction upgrades on the TUI (not a visual redesign): a
genuinely fuzzy command palette, language-aware diff highlighting,
scrollback + searchable history, a real live multi-task benchmark
dashboard, a reasoning-vs-action visual grammar, and completion
notifications. CLI-side; no cross-module contract changed. Detail in
`cli/AGENTS.md`.

- **Real command palette (ctrl+p)** — `cli/fuzzy.py` (dependency-free
  fzf/VS Code-style subsequence matcher) drives one ranked search over
  commands + custom commands + recent sessions + repo files in a
  scrollable OptionList; `git ls-files`-backed file scan, ancestor-repo
  guarded, cached per repo. No-arg commands run on choose; files and
  arg-taking commands prefill.
- **Syntax-highlighted diffs** — `ui.diff_render_lines`/`diff_text`:
  pygments tokens (language from the `+++ b/<file>` header) under the
  +/-/@@ diff roles, one renderer for the TUI inline preview, the
  approval modal, /diff and `vex fix`; degrades to flat roles when the
  language is unknown.
- **Scrollback + search** — the transcript's auto-follow pauses when you
  scroll up and resumes at the bottom; `/feed` opens the whole run's
  trace feed scrollable+searchable; `/sessions` is now a searchable,
  filterable browser (status:/repo:/since:/resumable + free text) over
  the full history, shared with the REPL.
- **Live multi-task dashboard** — `run-benchmark` shows one row per
  concurrent task: status · phase · elapsed · model calls · cost ·
  routing tier · model, folded read-only from each task's trace.jsonl +
  the runtime's model ledger (`runview.read_task_progress`) + the
  scheduler's `live_attempts()`; degrades honestly on the stub.
- **Reasoning vs action** — italic-dim commentary, bold-accent actions,
  one shared feed-style table for every surface; a terminal/OS
  notification on completion (TTY-only, VEX_NOTIFY=0 to silence).
- Verified: tests/test_cli_polish.py 38 (incl. the real-repo palette +
  300-session scale gates); full CLI sweep 480/480; evals --check 12 OK.

## Mid-Task Interactive Steering round (2026-09-14)

Steering a live task — new instructions mid-run without losing
progress, the way Claude Code accepts a message mid-response. New
module `harness/steering.py`; loop integration in `harness/core.py`;
REPL reader + live-run registration + TUI wiring in the CLI.

- **Task A — inject mid-task**: while a fix/build runs, plain typed
  text becomes a steering instruction (logs/{task_id}/steering.jsonl
  — an append-only journal ANY process can append to: the REPL's
  reader thread, the TUI, a second terminal). Guide events land in the
  live bash session at the next turn boundary as a USER STEERING
  message; conversational input is answered inline, never injected.
- **Task B — state machine defined interaction**: a steering interrupt
  is a non-transition audit event (record_event) — the trail shows WHEN
  the user redirected without corrupting any valid edge. Strong intents
  ride existing forward edges only: replan = editing → repairing →
  planning (work-in-progress KEPT; attempt slot returned), abort =
  clean resumable stop landing in failed.
- **Task C — guarantees held**: pending steering at the final gate OR
  the new pre-mint re-check BLOCKS success minting — a steered task
  still cannot claim success without a real verifier pass after the
  steering is incorporated. The journal replays on resume: steering
  that arrived before a crash is still pending on the resumed run.
  The OFF arm (`steering_enabled: false`) never polls the journal.

All pinned by tests/test_steering.py (41/41, including Docker-gated
e2e through the real loop) + the TUI behavior tests; evals gates
(--check, --quick) green with no regressions.

## Proactive Codebase Health Scan round (2026-09-14)

`vex scan` — unprompted, read-only codebase analysis. No bug report
needed: the harness applies its existing structural knowledge (code
graph) plus stdlib AST and dependency-manifest analysis to surface
what's actually worth attention, then hands any finding off as a real
verifier-gated task.

- **Task A — `vex scan --repo .`**: coverage gaps (untested
  load-bearing modules/functions via the code graph), latent-bug code
  smells (mutable defaults, bare except, swallowed exceptions), and
  dependency pins (cross-manifest conflicts offline; opt-in PyPI
  freshness via `--remote`). Read-only by construction — no shell, no
  sandbox, no model calls; findings and report only (`logs/scan-<id>/`
  with scan.json + report.md + trace).
- **Task B — ranked, not dumped**: every finding is scored (severity ×
  structural load: fan-in, call sites) with a grounded rationale each
  (the fix-round rationale-log style); the report shows the top 8
  (`scan_max_findings`), the rest stay in scan.json behind
  `--max-findings`. A scan that dumps 50 low-value findings is worse
  than one with 5 real ones — the caps are the product decision.
- **Task C — close the loop**: `vex fix --finding <scan_id>#<n>` (or
  `vex scan --fix N`) turns "Vex noticed this" into a REAL fix/build
  task through the existing verifier-gated entries — the suggested
  test file is the target for coverage/smell findings, build mode for
  dependency bumps.

Validated against this repo itself: 7 focused findings (4 coverage
gaps, 3 swallowed-exception smells), every one genuine — after the
noise-budget iteration (early runs surfaced 1323 raw smell sites).
Read-only invariants, hostile finding-ref containment, and the
scan→fix e2e through real Docker verify are all test-pinned
(tests/test_scan_mode.py, 50/50).

## CLI citizenship round (2026-09-14)

The release-readiness pass: the basic behaviors every professional CLI
has. All additive; no existing flag or output changed meaning.

- **Task A — `--help` / `--version`**: `--help` now shows every public
  subcommand, the key flags, and the exit-code contract in one clean
  overview (epilog + `RawDescriptionHelpFormatter`; hidden machinery
  like the completion backend stays hidden on Python 3.10 where
  argparse's SUPPRESS has the `==SUPPRESS==` bug). `vex --version`
  prints the installed dist version with a pyproject-matching
  source-tree fallback — pinned to pyproject.toml by test.
- **Task B — meaningful exit codes**: the old 0/1/2 contract is
  preserved exactly, and two failure categories were split out of the
  catch-all 1: **3 environment error** (Docker/sandbox/dependency) and
  **4 model/network error** (litellm/endpoint/auth/rate-limit). New
  `cli/exit_codes.py` is the single numeric source of truth
  (`EXIT_CODES` table + a never-raising classifier over the same layer
  mapping `cli.errors` documents); the plain-language explainer now
  prints the category + code next to its diagnosis. `vex fix`,
  `vex run-benchmark`, and the top-level safety net all classify.
  130 (Ctrl+C) unchanged.
- **Task C — NO_COLOR / `--no-color`**: already honored via rich; now
  pinned end-to-end by tests (env var and late flag both strip every
  ANSI code from real command output).
- **Task D — shell completion**: `vex completion bash|zsh|fish|
  powershell` prints a completion script; `--install` writes it to the
  shell's conventional location (bash-completion dirs, oh-my-zsh/fpath,
  fish vendor_completions, the PowerShell `$PROFILE` — idempotent via
  marker). The scripts are DYNAMIC: they call a hidden
  `vex __completions` backend that derives candidates from the real
  argparse parser (subcommands, flags, nested subcommands, choice
  values like `--tier global|project|local`), so completions never
  drift from `--help`. README documents install for all four shells.
- **Task E — `vex update`**: self-update that detects the install
  method (pipx / the installers' dedicated `~/.vex-venv` / plain pip /
  source checkout) and re-runs the matching upgrade command; source
  checkouts get the honest `git pull` + `pip install -e .` recipe.
  `--check` reports installed vs latest (GitHub tags first, PyPI JSON
  fallback — the probe that found this environment's GitHub API edge
  is filtered); an unreachable network is itself exit 4 per Task B.
- **Task F — clean uninstall**: `vex uninstall` enumerates everything
  Vex created on the machine (pipx venv note, installer venv + shims,
  PATH entry, config roots — probed via the same layout functions the
  installers use), shows the list, requires confirmation (or `--yes` /
  `--dry-run`), removes the Windows user-PATH entry via the registry
  API, and prints the pip/pipx one-liner for the package itself. Full
  manual recipe also documented in the README.
- **Task G — `--json` output**: `vex fix --json` and
  `vex status --task-id <id> --json` emit machine-readable documents
  (status, attempts, cost, verification flags, diff, trace path,
  exit-code reason) with zero theme markup and no spinner — the whole
  stdout is the document. Verified parse-able end-to-end through the
  real offline fix e2e (scripted model through real run_task + Docker
  verify).
- **Verified**: NEW tests/test_cli_release.py (50) + updated
  tests/test_cli_errors.py pins; full CLI sweep green in one run
  (test_cli 16, test_cli_vex 10, test_cli_vex2 24, test_cli_vex3 49,
  test_cli_errors 13, test_cli_release 50, test_cli_adversarial 62,
  test_cli_config 42, test_cli_plugins 30, test_cli_tui 20); ruff: all
  NEW files violation-free, main.py held at its pre-existing baseline
  count. One real defect found live and fixed: litellm's error banner
  prints to stdout, which polluted `--json` documents — the run's
  stdout is now diverted to stderr in JSON mode.

## Repo & legal hygiene round (2026-09-14)

The public-repo table stakes, as one commit: MIT `LICENSE`; `SECURITY.md`
(private reporting channels, supported versions, an honest in/out-of-scope
map of every attack surface this codebase runs — sandbox, MCP server,
CLI, host-side installers, CI — plus pointers to the existing
adversarial-round evidence so reporters don't re-probe held ground);
`CODE_OF_CONDUCT.md` (Contributor Covenant v2.1, verbatim + contact
point); issue templates (bug / feature, `config.yml` routing security
reports away from public issues and linking discussions) and a PR
checklist template encoding the repo's two load-bearing rules (verifier
gate, original-repo-never-mutated) plus module-contract and lint-ratchet
hygiene. Set via the GitHub API: repo description, 12 topics,
discussions, Dependabot security updates + vulnerability alerts (20
pre-existing dependency alerts now surfaced and trackable). The social
preview image (`.github/social-preview.png`, 1280x640 brand card,
oxblood on ink) is generated and committed, but its upload is
UI-only — one manual step left:
repo Settings → Social preview → upload that file.

## Install round (2026-09-13)

Vex is now curl-installable — no PyPI package needed. Three one-line
installers live at the repo root and are reachable at predictable
raw.githubusercontent URLs (see README "Install"):

- `install.sh` — macOS / Linux / WSL: pipx if available (isolated,
  per-app venvs), else a dedicated `~/.vex-venv` + `~/.vex/bin` shims;
  idempotent profile PATH hints; friendly failures for missing/old
  Python, missing git, and the Windows-Python-under-Git-Bash case
  (redirects to the Windows installers).
- `install.ps1` — Windows PowerShell (5.1-compatible; `irm | iex`):
  same logic; python.org directory-scan fallback for "installed
  without Add-to-PATH"; USER-path registry write (idempotent, never
  setx — it truncates at 1024 chars).
- `install.cmd` — Windows CMD: same logic; `py -3` launcher probe;
  only `exit /b` (never closes the caller's terminal); CRLF-committed
  (cmd's label scanner breaks on LF — pinned via `.gitattributes`).

Supporting changes: `vex --version` (the installers' verification
step; importlib.metadata with a source-tree fallback), packaging fix
(flat-layout multi-package builds now specify the package list
explicitly — `pip install .` from the repo was broken before), dist
name `vex-agent-cli` (console script remains `vex`).

All three verified live: WSL (venv + pipx-contract + re-run
idempotence + failure paths + Git Bash guard), PowerShell 5.1 (file +
`iex` flows, registry writes), CMD (full download-and-run flow,
same-session PATH).

## v0.1.0 (2026-09-09)

Initial tagged release: the full four-layer system, built and validated
end-to-end.

### The four layers, integrated

- **Harness** (`harness/`) — planner → step-agent → verifier-gated
  completion. Bash-only agent loop (mini-swe-agent style) with
  per-step fresh sessions, constraint re-injection, protected paths,
  RECALL (on-demand reinjection of compacted context from the task
  trace), checkpoint/resume at step granularity, and product-grade
  output on verified fixes: git branch/commit/PR description +
  grounded rationale.md. Success is only ever *claimed* when the
  target test passes AND the full suite shows no regressions — the
  verifier gate is absolute.
- **Execution** (`execution/`) — Docker sandbox: fresh `--rm`
  container per command, networkless by default, read-only rootfs,
  `--cap-drop ALL`, mem/cpu/pids limits; stateless three-valued
  verification with flake detection; orphaned-container reaping;
  cross-process image-build locking (proven at 40–50 concurrent).
- **Runtime** (`runtime/`) — process-per-task scheduler proven at
  45 concurrent tasks with 8 simultaneous mid-run hard kills
  (45/45 success, 8/8 genuine resumes, zero leaked containers);
  checkpoint/resume across kills; approval gate; per-call cost ledger.
- **Memory + MCP** (`memory/`, `mcp_server/`) — tree-sitter code
  graph + SQLite decision memory (auto-ingesting every task's
  structured state at any depth), exposed as 5 MCP tools over stdio
  to any MCP client (Claude Code, Cursor, …), plus an MCP client for
  consuming external servers.

### The novel mechanism: adaptive model routing (measured)

Per call, the runtime predicts difficulty from intrinsic signal (issue
text) + struggle signal (conversation tail) and routes easy/medium
calls to a cheap model, hard calls to an expensive one. Ablations on
real bugs, real models, full real stack:

- 5 fixture bugs: same 5/5 success at **45% of baseline cost**
- 16-task expanded set (varied bug classes + issue styles): 16/16
  both arms at **39% of baseline cost**, 3.3x faster wall
- 5 real OSS repos (more-itertools, arrow, inflect, boltons,
  python-semver): adaptive 3/5 vs always-expensive 2/5 at **24% of
  the cost** — the routing margin holds on unfamiliar multi-hundred-
  K-token repos

Honesty notes: free-tier endpoints, proxy price rates, single reps —
directional, not benchmark-grade. Details and artifact paths:
`README.md`.

### Validation beyond the fixture set

- jaraco/path (unfamiliar real OSS repo): full-DoD run — verifier-gated
  success in 1 attempt with a real cloud model ($0.053, 6 calls),
  git output, approval gate, memory ingestion. The first attempt's
  honest failure exposed and fixed a real harness bug (binary-artifact
  diff crash) — kept as part of the record.
- python-semver: module DoD, 15/15 checks (baseline → broken-state
  detection → in-sandbox fix → verified, flake-flagging, git output).
- Runtime stress: 45 tasks @ concurrency 45 with 8 mid-run kills —
  all resumed, zero leaks.
- Sandbox adversarially confirmed: 24/24 sequential + concurrent
  attack suites (escape, resource exhaustion, cross-container probes)
  — every isolation property held.

### Security hardening (Round 6)

Adversarial passes over the MCP server, CLI, and sandbox. One real
data leak found live (path traversal in `task_status` /
`harness status --task-id` on Windows drive-letter semantics), fixed
via a shared semantic task-id guard (`memory/paths.py`), and pinned
by 101 adversarial tests across the MCP + CLI boundary suites.
Full probe matrices and outcomes: `mcp_server/AGENTS.md`,
`cli/AGENTS.md`, `execution/AGENTS.md`.

### CI (Round 7)

- `.github/workflows/ci.yml` — harness + runtime suites across the OS
  matrix, light per-push stress, nightly full stress + adversarial
  abuse suites.
- `.github/workflows/memory-cli-ci.yml` — memory/MCP/CLI/dashboard
  suites, including the real stdio MCP-client round-trip, across
  Linux/Windows/macOS × Python 3.10/3.12. Includes a pinned
  regression for the SDK `errlog` import-time-binding flake fixed
  this round.

### Known limitations (documented, not hidden)

- Failure classification is deliberately not implemented (the chosen
  novel mechanism was adaptive routing, not repair strategy — the
  `last_feedback` seam remains where it would plug in).
- Free-tier endpoints made some Round-6 multi-repo runs flaky
  (empty/timeout model responses) — the harness refused to claim
  success in every such case; final sweep numbers pending endpoint
  stability.
- SWE-bench Lite deferred to Phase 6 per spec. Python-only. stdio-only
  MCP transport. No web UI beyond the read-only dashboard.

### Pre-history (Rounds 1–5, untagged)

Round-by-round build history — initial contracts and stubs
(`INTERFACES.md`), each module's Phase-1 milestone, integration of
the real stack (real Docker sandbox, real scheduler, real cloud
model), resume/checkpoint contract, adaptive-routing ablations at
two scales, demo packaging — is preserved in each module's
`AGENTS.md` and the git history. `demo/run_demo.py` tells the whole
story offline in one command.
