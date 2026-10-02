# Changelog

High-level milestones of the build, newest first. This is a readable
summary of what the system can do at each stage — not a commit log.
Per-module detail lives in each module's `AGENTS.md`; the cross-module
contracts in `INTERFACES.md`; the full plan in `project-spec.md`.

## The rename: Vex -> Neo

The agent is now **neo**. The domain is **neo-agent.si**. The distribution is
**`neo-agent-cli`** — install it with `pip install neo-agent-cli`.

The name `neo-agent` was the intended distribution name and is **already taken**
on PyPI by an unrelated 2018 Django USSD package (`neo-agent` 0.1.3, owner
`deone`), and PyPI does not release a claimed name. `neo-agent-cli` was on the
availability shortlist from the start and is verified **available**. The
earlier working name `neo-harness` was **never uploaded**, so this change needs
no transfer, no migration, and breaks no existing install.

This is a full rename, not a rebrand: the command, the distribution, the
module and identifier names, the environment variables, the on-disk
directories, the rich/Textual style prefixes, the TUI widget ids, the
protocol header names, and the documentation all moved. The previous
spellings are still **read**, never written.

| | now | also still accepted (read-only) |
|---|---|---|
| console script | `neo` | `vex`, `harness` |
| distribution | `neo-agent-cli` | `vex-harness` |
| per-repository state | `<repo>/.neo/` | `<repo>/.vex/` |
| user state root | `~/.neo` (or the platform data dir) | `~/.vex` |
| environment | `NEO_*` | `VEX_*`, `HARNESS_*` |
| design contract | `NEO_DESIGN_SYSTEM.md` | - |

What is deliberately **not** renamed: the historical round identifiers
(`VEX-CS-01`, `VEX-CEILING-12`, `VEX-PF-05`, ...) and the archived
`VEX_*_MASTER_PROMPTS.md` prompt files. They are records of past rounds,
and rewriting them would desynchronise them from the round ids the
handoffs cross-reference. One PyPI-name-availability sentence also keeps
its historical candidates (`pyvex`, `vexcli`, ...): that sentence is a
record of which distribution names were unavailable.

### Backwards compatibility, and where it lives

`shared/brand.py` is the single authority: the canonical names, and
`apply_legacy_env()`, which is called once per process from
`cli.main.main`, `mcp_server.server.main`, `dashboard.server.main` and
`evals.run.main`. It is generic over the `VEX_` -> `NEO_` prefix, so a
`NEO_*` variable added in future is honoured from its legacy spelling with
no code change - a hand-maintained compat table would rot.

Four rules the fallback holds, each pinned in `tests/test_brand_rename.py`:

1. **New is written, old is read.** Nothing writes `VEX_*`, `.vex/` or
   `vex-harness` any more.
2. **The new name always wins.** If both `NEO_HOME` and `VEX_HOME` are
   set, the new one is used and the legacy one ignored - otherwise a user
   who renamed the variable and still exports the old one would silently
   keep the old root with no way to tell which one the product used.
3. **The fallback is a read path.** It never migrates, copies, renames or
   writes into a legacy location. Silently moving a user's configuration
   is how a tool loses it.
4. **The project-directory walk is bounded to the enclosing git
   repository.** An unbounded walk reaches `$HOME`, and on a host whose
   `$HOME` is a dotfiles repository that made a user-level directory the
   project tier for *every* unrelated project. This also fixed the
   pre-existing red in `tests/test_cli_config.py::test_no_project_dir_found`.

The on-disk fallbacks live with the resolvers that own them, not in the
brand module: `memory.paths.neo_home()` (honours `NEO_HOME`, then
`VEX_HOME`, then `HARNESS_HOME`, then the platform data dir - falling back
to the previous directory name only when the current one does not exist),
and `cli.neoconfig.project_settings_dir()` / `global_settings_path()` /
`legacy_settings_path()`.

`cli.capability.installed_version()` and `cli.main._get_version()` both
look the version up through the same list, so a machine with
`vex-harness` installed resolves a real version instead of reporting
`(unknown)`. The list is read from `shared.brand` rather than written out
in `cli/main.py`: a hardcoded list is how a rename silently reports "this
build has no version".

### The TUI wordmark

Same style, new letterforms: 6 rows, ANSI-shadow box drawing, the same
9/8/8-column cell geometry and the same 26-column ljust'd span, so the
per-column gradient ramp and every downstream width budget are unchanged.
The `'#'` ASCII fallback for legacy consoles is redrawn to match, and both
variants are pinned on their *distinctive* fragments (N's diagonal, E's
bars, O's bowl) rather than on geometry alone.

### The site

`site/` now defaults to `https://neo-agent.si` instead of
`http://localhost:3000`. That default was a silent SEO failure waiting to
happen: a build that forgot `NEXT_PUBLIC_SITE_URL` shipped `localhost`
into every canonical, every sitemap entry and every social card. Each route
declares its own canonical, and the root layout deliberately declares
none - an inherited canonical would make all 22 pages duplicates of `/`.
`vercel.json` gains a `www.` -> apex redirect and HSTS.

## v0.3.0 (unreleased source candidate — NOT published, NOT tagged, NOT ready to publish)

**Nothing in this entry has been published, tagged, pushed, or uploaded.**

> ### ⛔ Do not publish this yet: the new default engine scores 0/10
>
> A parallel gate ran both agent engines against the same 10 real bugs in 5
> real open-source projects ([`docs/release-verdict.md`](docs/release-verdict.md)):
>
> | engine | score |
> |---|---|
> | `daily` — the new default below | **0 / 10** |
> | `legacy_agent` — the 0.2.0 default | **10 / 10** |
>
> One cause: the kernel's security policy refuses to let the agent **read** the
> test file that defines whether the bug is fixed, because it matches a
> protected `tests/` pattern and the refusal is applied to reads as well as
> writes. The agent tries to fix a failing test, is told it may not look at the
> test, and stops.
>
> **The breaking change below moves every user onto that path.** Note also that
> the 14/14 prompt-regression matrix and the 700 passing Round 2 tests do not
> catch this, because neither exercises the kernel against real defects.

### Why this is 0.3.0 and not 0.2.1

The unpublished 0.2.1 candidate was superseded before it ever reached PyPI,
and the tree it would have shipped now contains a breaking default switch
(below). Publishing that as a patch increment would tell an existing 0.2.0
user "nothing important changed" when the default engine, the trace event
kinds, and the on-disk run artifacts all changed. Full reasoning and the
exact owner runbook: [`docs/release-evidence.md`](docs/release-evidence.md).

### ⚠ BEHAVIOUR CHANGE — the daily agent now runs the `daily` engine by default

**This is a breaking default switch and it is not silent.** An unqualified
`harness.agent_loop.run_agent` call previously forced `legacy_agent`; it now
resolves through the kernel's own strategy resolver and gets **`daily`**.

Why: the forced `legacy_agent` made the resolver's `daily` default unreachable
in practice, so `ConversationMemory`, `TurnLedger`, the context budget with
reversible compaction, `KnowledgeContext` + LSP, exact three-way rewind, and the
kernel recovery path were all unreachable for the daily user. Measured on this
tree, an unqualified run now writes `conversation.jsonl`, `turns.jsonl`, and
`context.json` next to its journal; a `legacy_agent` run writes none of them.

**What an existing user may notice**

- A different engine answers, so trace event kinds, the reply protocol the model
  is asked for, and the on-disk artifacts under `logs/{task_id}/` differ.
- A finished run with no declared verifier still reports
  `completed_unverified` and never `success` — that invariant is unchanged, and
  is now what the daily-driver probes assert instead of pinning `success`.
- The `AgentResult` dict is unchanged in shape except for two additive keys,
  `agent_strategy_source` and `agent_strategy_resolver`.

**How to keep the old engine** (the compatibility path is fully supported, not
deprecated):

| want | do this |
|---|---|
| pin one call | `config={"agent_strategy": "legacy_agent"}` |
| pin every call in a project | set `agent_default_strategy = "legacy_agent"` in `.neo/settings.toml` |
| pin from code | `from harness.agent_loop import run_agent_legacy` |

`agent_default_strategy` defaults to `None` = "no override", so adding the key
changed no existing task. `"none"` and `"default"` also mean "no override", so a
settings file can turn it off without deleting the key.

Every run's result now states which engine produced it
(`agent_strategy` / `agent_strategy_source`), and the run journal's
`run_started` / `strategy_selected` rows carry the same pair.

### What else changed in this round

Verified-fix integrity and honesty, all of it gated by named tests:

- **The flake gate can fire.** It previously could not: the default
  configuration computed `run_count == 1`, and a single run cannot be flaky.
  Repetition count and observed outcomes are now recorded in the result and the
  trace, and a single-run configuration reports `not_run` rather than a
  `flaky: false` that reads like "we checked and it was stable".
- **Baseline failure set and environment triage.** "Regression" now means
  *newly* failing; a test that was already broken is reported as pre-existing.
  Environment failures (missing interpreter, unresolvable declared dependency,
  absent Docker daemon) are classified and end the run instead of sending the
  agent to "repair" the machine.
- **Test configuration is protected.** `conftest.py`, `pytest.ini`, `tox.ini`,
  `setup.cfg`, and `[tool.pytest.ini_options]` join the protected-path set, and
  a change in the *effective* pytest configuration is surfaced as a first-class
  `test_config_changed` finding.
- **Zero collected tests is never a pass.** New `no_tests_collected` outcome,
  per-language ecosystem registry, and a fed JUnit XML result channel.
- **Editing correctness.** Ambiguous matches are a refusal listing candidates,
  CRLF and non-UTF-8 encodings round-trip, a failed post-edit check rolls the
  file back byte-for-byte, and writes are atomic.
- **Scale.** The per-file symlink walk is hoisted to a per-root set lookup, the
  source-digest pass is stat-gated, and PageRank is seed-restricted. Measured
  before/after curves are in `harness/AGENTS.md`.
- **Cost and quota.** Per-call budget enforcement (was per-attempt), quota
  exhaustion separated from rate limiting, and the provider backoff exempted
  from the hang watchdog so a backing-off provider is not killed as hung.
- **Trust.** The daily interactive path is sandboxed by default with a loud
  receipt when it is not; the unconditional `global` approval bypass is gone;
  an empty command prefix is refused instead of matching everything.
- **Adaptive routing: an honest negative.** A structural difficulty predictor
  beat the incumbent on held-out data, but that split carried **1** hard-labelled
  observation against a declared floor of 3. It was therefore **not shipped**;
  the incumbent stays the default. Details in `docs/benchmark.md`.

### Verification lanes for this release — read this before trusting a number

| lane | status | what actually ran |
|---|---|---|
| Docker sandbox + verifier | **ran** | Docker 28.5.1. Full 14-task × 8-arm prompt-regression matrix through the real sandbox and verifier: **CLEAN, 98/98 valid comparisons, 0 regressions, 0 errors**. |
| Host self-check (`--check`) | **ran** | 14/14 tasks validated: fails-pre-fix, target-green-post-fix, suite-green-post-fix. |
| Round 2 regression suites | **ran** | 14 files, **700 passed, 2 skipped**. |
| ACP protocol adapter | **ran** | `tests/test_acp.py`: 29 passed, real stdio and in-memory transports. |
| Real attached-PTY accessibility probe | **ran, 2 of 3 profiles green** | Under a real allocated PTY: `xterm-256color` 21/21, `NO_COLOR=1` 21/21, **`TERM=dumb` 20/21 — modal open 254.763 ms against a 250 ms gate.** |
| Live model provider | **BLOCKED — not run** | Credential present; TLS 1.3 handshake to `api.anthropic.com` succeeds; the API answers **HTTP 401 `authentication_error`**. **No live model-quality evidence exists for this tree.** |
| Native Windows ConPTY | **BLOCKED — not run** | `CreatePseudoConsole` returns FALSE in this session (no interactive window station). The WSL POSIX PTY is the real-terminal evidence instead. |
| Manual repair by a human | **BLOCKED — not run** | Three real-OSS samples exist (`parse`, `bottle`, `click`) with scripted models through real Docker, but **none carries an explicit human manual-repair boolean**, so the lane is blocked rather than inferred. |
| Full repository test suite | **NOT RUN — by design** | Three other terminals are running suites in this shared tree. A full-suite result against a moving tree is not a gate; the project's own standard is that it must be run on a quiet tree. |
| Clean reproducible build | **NOT RUN** | The tree is dirty (368 changed paths), so there is no candidate SHA to build from. |
| Lint ratchet | **RED** | 3 files carry new debt (`head_iv.py`, `measure_batch_phase.py`, `tests/test_cli_theme.py`); none owned by this round. Baseline deliberately not updated. |

Four blockers are open and named, with reproducers, in
[`docs/known-issues.md`](docs/known-issues.md). Two of them (`SG-01`, `SG-02`)
mean the `daily` default switch above ships with a known defect on the kernel
path, and `SG-01` is the 0/10 result at the top of this entry. That is stated
here rather than in a footnote.

### Release and adoption artifacts

- [`docs/benchmark.md`](docs/benchmark.md) — the public benchmark: methodology,
  honesty caveats, and per-task detail **including every failure**.
- [`docs/release-evidence.md`](docs/release-evidence.md) — the lane-by-lane
  evidence table and the owner runbook. Publishing stays owner-only.
- [`docs/accessibility.md`](docs/accessibility.md) — measured terminal
  accessibility status, the real-PTY receipt, and the two open gaps.
- [`docs/onboarding.md`](docs/onboarding.md) — install to first verified fix.
- [`docs/known-issues.md`](docs/known-issues.md) — the bug corpus: open
  blockers with reproducers.

## v0.2.1 (superseded, never published)

**This version was never released.** The source tree carried `0.2.1` from
2026-09-24 until 2026-09-27, when it was superseded by `0.3.0` (see above).
No 0.2.1 artifact was ever uploaded to PyPI, so there is nothing to install
under that number and this section is kept only so the history is not
rewritten. The description below records what that candidate contained.

This version is not published yet; the public PyPI latest remains 0.2.0.
It ships the whole daily-use CLI surface that the 0.2.0 wheel predated
(built before these files landed): first-run onboarding (`neo login`
wizard incl. TUI modal, `neo logout`, `/model`, exit-4 gate),
`.neo/` repo scaffolding, full-screen TUI, plugin enable/disable +
`neo mcp` registry + `neo skills`, `/plan /review /compact /copy-diff
/history /trace /feed /steer` parity, fuzzy palette,
syntax-highlighted diffs, live benchmark dashboard, `neo
analyze-history`, completions, self-update, and `neo uninstall`.
No model set + `neo` -> inline wizard (once), saves, never asks
again: `neo login` (Official OpenAI/Anthropic/Gemini + Router
OpenRouter/TokenRouter/Ollama/Custom with free-text base_url/model,
live-tested before saving), `neo logout`, `/model`, TUI modal
variant, flag-command gate (missing model exits 4, never prompts),
project files can never hold secrets, settings chmod 600 (POSIX).

The release-candidate packaging pass declares MIT metadata, requires the
MCP 2.x API actually used, includes the new runtime subpackages, and
prevents generated fixture bytecode from entering either artifact. A
fresh disposable build passed the repository artifact verifier and
`twine check` for exactly one 0.2.1 wheel and sdist.

The 0.2.1 candidate now includes the complete `agent_sdk` package in both
artifacts. A clean wheel-only environment outside the checkout imports the
wheel's SDK, runs a deterministic `LocalAgent.query()`, and replays its
contiguous versioned event journal. On Windows, `neo uninstall --yes` detects
the outer console launcher, hands its live process handle to a detached helper
using an interpreter outside the active venv, waits for the launcher to exit,
then runs pip through the target venv. The exact-candidate installed-wheel
suite verifies the helper receipt, launcher PID, both removed entry points,
and removed distribution metadata.

The reproducible delivery gate now uses a universal hash-locked
`uv.lock`, exact release-tool pins, Python 3.10–3.12 across all three
supported operating systems, complete collection evidence, real Docker
required lanes, wheel/sdist clean-room installs, native installer
resolution checks, sdist-to-wheel reproduction, CycloneDX SBOM and
SHA-256 artifacts, and an aggregate job that fails when any required
lane is skipped. Artifact upload paths are exact and no publish action
is configured. Release scripts expose schema-versioned JSON/NDJSON and
stable exits. `scripts/github_workflow.py` adds fixed-argv, redacted,
read-only issue-to-task and PR-review packets; it cannot approve,
comment, merge, push, or upload. Git-native output is transactional and
redacts common credential forms from commits and PR descriptions.

Publishing is owner-only and must start from a reviewed clean tag. The exact
command sequence, with the pre-flight lanes it depends on, is
[`docs/release-runbook.md`](docs/release-runbook.md). It is deliberately not
restated here: a changelog that carries a copy-pasteable upload command is a
changelog that eventually gets the command run against the wrong version.

- **Release verification (2026-09-24):** rebuilt the current source
  candidate into two clean timestamp-controlled artifact sets. The
  normalized wheel and sdist are reproducible across both builds and
  pass `twine check`; the verified wheel SHA-256 is
  `bcc7569f22ba6473f1c816d8a87b652858ea55390b4ffd4da6bffeea9dc1370b`
  and the normalized sdist SHA-256 is
  `d83f5f7d23ba09bbb0bd38d5c707e02d44c2a64e54f3bab2f41b3dd1bca8af8f`.
- **Clean-room matrix (2026-09-24):** Python 3.10.11, 3.11.15, and
  3.12.13 each passed separate wheel and sdist installs, `pip check`, all
  configured top-level imports, `neo`/`harness` version and help, the
  packaged smoke benchmark, a real Docker-backed scripted fix, trace
  verification, and source/package/original-fixture integrity checks:
  96 checks passed, with zero failures, blocks, or skips.
- The dependency floor now requires `mcp>=2.0,<3`; the public PyPI
  release remains 0.2.0 and publication of 0.2.1 is still owner-only.
- **Resume identity closure (2026-09-25):** runtime and strict-kernel
  checkpoints now bind canonical repository, exact-request, revision, and run
  namespace identity; mismatches fail closed instead of resuming stale work.
- **Final integration diagnostic (2026-09-25):** the 0.2.1 wheel and sdist
  include executable help surfaces for `harness`, `cli`, `runtime`,
  `execution`, and `evals`; all three installers and the README now enforce
  the metadata range Python 3.10-3.12. Fresh external Python 3.10 installs of
  both exact artifacts pass `pip check`, console/module help and version,
  and the packaged smoke benchmark. Diagnostic artifact SHA-256 values are
  `0c6ceb98794aac6e01b56b8576e6f02624ebe54f07cbec5a840a5b5f2fba40fd`
  (wheel) and
  `e1b75d215e8db6441f48c5337ece534fb9b3cbb00303ad255f0d40dd8501c8de`
  (sdist). These are not a publishable candidate: the source tree is dirty,
  the repository-wide lint ratchet and full-suite completion are not green,
  daily-driver readiness exits 2. No upload, tag, commit, or push was performed.

## Documentation, dogfooding, and product polish (2026-09-25)

- Added the current daily-use documentation set under `docs/`: first-run
  quickstart, provider/router configuration, TUI/REPL command reference,
  plan/build/fix/question workflows, permissions and Docker boundaries,
  skills/plugins/connectors, sessions/checkpoints/resume, headless/CI/SDK
  use, troubleshooting, architecture/event schema, feature matrix, and
  evidence-labeled dogfood reporting. The README now links to the guide and
  distinguishes the live interactive agent from the verifier-gated `neo fix`
  path.
- `demo/run_demo.py` now bounds its structural-memory example to the isolated
  demo repository instead of indexing the entire checkout. The offline fix
  demo completes reproducibly; `demo/agent_demo.py` covers question, plan,
  approval, edit, undo, resume replay, and compaction.
- Fresh verification: `python demo/run_demo.py` and `python demo/agent_demo.py`
  exit 0; the full 26-case × 2-arm daily-driver run passed 52/52 arms and its
  real Docker canary. The run was intentionally `NOT_READY`: live-provider
  was not selected in the baseline run, the explicit provider retry was
  blocked by model access, and three historical samples lacked explicit
  manual-repair booleans. Zero false verified successes, unauthorized
  mutations, and lost edits were observed; cost, latency, token, and
  intervention metrics are recorded in `docs/dogfood-report.md`.
- No Docker, provider, or skipped lane is reported as passed. Source-only SDK
  packaging and the LSP daily-driver boundary remain explicit limitations.

## v0.2.0 (2026-09-21)

The daily-driver release: `pip install neo-harness` (the distribution was named
`neo-harness` in this round; it was **never published**, and the name was later
changed to `neo-agent-cli` — see the rename entry at the top of this file) and
the one-line installers now always land on the same working build.

- **Packaging** — version 0.2.0; the missing runtime deps are declared
  (`pygments` for the language-aware diff renderer, `tomli` for TOML
  settings on Python 3.10; Docker stays a system requirement, not a
  pip dep — the sandbox shells out to the Docker CLI; `psutil` stays
  undeclared — dev-only soak tooling with guarded lazy imports).
  `litellm==1.74.9` pin kept (newer breaks the `typing` import on
  3.10). `cli/fixtures/smoke_repo` now ships inside the wheel/sdist
  (`[tool.setuptools.package-data]`), so `neo run-benchmark --subset
  smoke` works from a pip install instead of failing with "smoke
  fixture repo missing". `neo --version` stays pinned to
  pyproject.toml by test.
- **Installers** (`install.sh` / `.ps1` / `.cmd`) — PyPI-first:
  `pipx install neo-harness` / `pip install neo-harness` by default (the
  distribution name of this round; it is now `neo-agent-cli` — see the rename
  entry at the top of this file);
  `NEO_INSTALL_SOURCE` (or an explicitly-set `NEO_INSTALL_REPO` /
  `_REF`) still pins a git checkout. Banners now read "the AI coding
  agent for your terminal". Preflight: Python 3.10+ (hard fail), git
  required only for git-URL sources, Docker warn-only. Post-install:
  `neo --version` + `neo update --check` (best-effort, never fails
  the install). Line-ending contracts unchanged (cmd/ps1 CRLF,
  sh LF — see `.gitattributes`).
- **Self-update** (`neo update`) — `--check` asks PyPI first, GitHub
  tags as fallback (the order the old code had was backwards for a
  PyPI-default world); upgrades run through the same method/source
  the install used; source checkouts still get the honest `git pull`
  refusal.
- v0.2.0 is the current public PyPI release. The v0.2.1 source candidate
  remains unpublished pending a clean reviewed tag and release-gate run.

## Neo TUI interaction-polish round (2026-09-16)

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
  approval modal, /diff and `neo fix`; degrades to flat roles when the
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
  notification on completion (TTY-only, NEO_NOTIFY=0 to silence).
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

`neo scan` — unprompted, read-only codebase analysis. No bug report
needed: the harness applies its existing structural knowledge (code
graph) plus stdlib AST and dependency-manifest analysis to surface
what's actually worth attention, then hands any finding off as a real
verifier-gated task.

- **Task A — `neo scan --repo .`**: coverage gaps (untested
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
- **Task C — close the loop**: `neo fix --finding <scan_id>#<n>` (or
  `neo scan --fix N`) turns "Neo noticed this" into a REAL fix/build
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
  argparse's SUPPRESS has the `==SUPPRESS==` bug). `neo --version`
  prints the installed dist version with a pyproject-matching
  source-tree fallback — pinned to pyproject.toml by test.
- **Task B — meaningful exit codes**: the old 0/1/2 contract is
  preserved exactly, and two failure categories were split out of the
  catch-all 1: **3 environment error** (Docker/sandbox/dependency) and
  **4 model/network error** (litellm/endpoint/auth/rate-limit). New
  `cli/exit_codes.py` is the single numeric source of truth
  (`EXIT_CODES` table + a never-raising classifier over the same layer
  mapping `cli.errors` documents); the plain-language explainer now
  prints the category + code next to its diagnosis. `neo fix`,
  `neo run-benchmark`, and the top-level safety net all classify.
  130 (Ctrl+C) unchanged.
- **Task C — NO_COLOR / `--no-color`**: already honored via rich; now
  pinned end-to-end by tests (env var and late flag both strip every
  ANSI code from real command output).
- **Task D — shell completion**: `neo completion bash|zsh|fish|
  powershell` prints a completion script; `--install` writes it to the
  shell's conventional location (bash-completion dirs, oh-my-zsh/fpath,
  fish vendor_completions, the PowerShell `$PROFILE` — idempotent via
  marker). The scripts are DYNAMIC: they call a hidden
  `neo __completions` backend that derives candidates from the real
  argparse parser (subcommands, flags, nested subcommands, choice
  values like `--tier global|project|local`), so completions never
  drift from `--help`. README documents install for all four shells.
- **Task E — `neo update`**: self-update that detects the install
  method (pipx / the installers' dedicated `~/.neo-venv` / plain pip /
  source checkout) and re-runs the matching upgrade command; source
  checkouts get the honest `git pull` + `pip install -e .` recipe.
  `--check` reports installed vs latest (GitHub tags first, PyPI JSON
  fallback — the probe that found this environment's GitHub API edge
  is filtered); an unreachable network is itself exit 4 per Task B.
- **Task F — clean uninstall**: `neo uninstall` enumerates everything
  Neo created on the machine (pipx venv note, installer venv + shims,
  PATH entry, config roots — probed via the same layout functions the
  installers use), shows the list, requires confirmation (or `--yes` /
  `--dry-run`), removes the Windows user-PATH entry via the registry
  API, and prints the pip/pipx one-liner for the package itself. Full
  manual recipe also documented in the README.
- **Task G — `--json` output**: `neo fix --json` and
  `neo status --task-id <id> --json` emit machine-readable documents
  (status, attempts, cost, verification flags, diff, trace path,
  exit-code reason) with zero theme markup and no spinner — the whole
  stdout is the document. Verified parse-able end-to-end through the
  real offline fix e2e (scripted model through real run_task + Docker
  verify).
- **Verified**: NEW tests/test_cli_release.py (50) + updated
  tests/test_cli_errors.py pins; full CLI sweep green in one run
  (test_cli 16, test_cli_neo 10, test_cli_neo2 24, test_cli_neo3 49,
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

Neo is now curl-installable — no PyPI package needed. Three one-line
installers live at the repo root and are reachable at predictable
raw.githubusercontent URLs (see README "Install"):

- `install.sh` — macOS / Linux / WSL: pipx if available (isolated,
  per-app venvs), else a dedicated `~/.neo-venv` + `~/.neo/bin` shims;
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

Supporting changes: `neo --version` (the installers' verification
step; importlib.metadata with a source-tree fallback), packaging fix
(flat-layout multi-package builds now specify the package list
explicitly — `pip install .` from the repo was broken before), dist
name `neo-agent-cli` (console script remains `neo`).

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
