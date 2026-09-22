# Vex

[![CI](https://github.com/Pavanteja2007/coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/ci.yml)
[![harness](https://github.com/Pavanteja2007/coding-harness/actions/workflows/harness-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/harness-ci.yml)
[![execution](https://github.com/Pavanteja2007/coding-harness/actions/workflows/execution-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/execution-ci.yml)
[![memory + MCP + CLI](https://github.com/Pavanteja2007/coding-harness/actions/workflows/memory-cli-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/memory-cli-ci.yml)
[![PyPI](https://img.shields.io/pypi/v/vex-harness)](https://pypi.org/project/vex-harness/)
[![Python](https://img.shields.io/pypi/pyversions/vex-harness)](https://pypi.org/project/vex-harness/)
[![License: MIT](https://img.shields.io/badge/License-MIT-9c6acc.svg)](LICENSE)

**Vex is an open-source, CLI-first AI coding agent that fixes real
software bugs end-to-end — and refuses to report success until the
repo's own test suite proves it.** One system where a Docker-sandboxed
agent loop, a concurrent checkpointing runtime, a persistent
cross-agent memory layer (exposed via MCP), and **adaptive model
routing by predicted difficulty** are integrated deliberately — the
integration is the point, not any one piece.

## Vex fixing a real bug, end to end

![Vex fixing mean() in a sandboxed repo copy](demo/vex-demo.gif)

That's the real product path: the repo is snapshotted (the original is
never touched), a planner decomposes the fix into verifiable steps, the
agent edits inside a locked-down Docker sandbox, and the final status
is minted **only by the verifier** — target test passes AND the full
suite shows no regressions — never by the model's own claim. Every run
leaves a branch + commit + PR description, a grounded `rationale.md`,
and a complete `trace.jsonl` of every prompt, command, and decision.

(The demo uses the offline scripted-model fixture so it's
deterministic; with a real model the same loop runs on your issue text.
Reproduce it yourself, no key needed: `python demo/run_demo.py`.)

## Quickstart

```bash
pip install vex-harness
```

```bash
vex fix --repo ./your-repo --issue "describe the bug and the failing test"
```

That's the whole core interaction. Requirements: Python 3.10+, Docker
(the sandbox), and any OpenAI-compatible endpoint + key. First run
with no model set offers the inline wizard (pick, endpoint, model,
key, live test, save) — or jump straight there:

```bash
vex login              # interactive wizard: Official (OpenAI/Anthropic/
                       # Gemini via litellm names) or Router (OpenRouter/
                       # TokenRouter/Ollama/Custom base_url + free-text
                       # model) — one tiny live call tests the creds
                       # before anything is saved; `vex logout` strips
                       # the key again; `/model` shows the effective model
```

### Auth: official models vs routers

`vex login` serves both shapes with the same flow (a failed test
retries — bad creds are never saved):

```bash
# Official: litellm's default endpoint, just a model + key
#   pick OpenAI -> model gpt-4o-mini (or any litellm name) + sk-...
#   pick Anthropic -> model claude-3-5-sonnet-20241022 + sk-ant-...
#   pick Gemini -> model gemini-2.0-flash + AIza...

# Router: any OpenAI-compatible base_url + whatever model it serves
#   pick OpenRouter -> https://openrouter.ai/api/v1 + openai/gpt-4o-mini
#   pick TokenRouter -> https://api.tokenrouter.com/v1 + z-ai/glm-5.3-free
#   pick Custom -> https://my-router.example.com/v1 + <any model name>
```

What lands where: `api_key` + `base_url` always go to the GLOBAL
settings file (never the committable project file — `vex config set
api_key --tier project` is refused); the model (+ provider) goes
global too unless `vex login --tier project`. Keys are masked in
`vex config list/get` (`sk-...<last4> (set)`), settings files are
chmod 600 where the OS allows, and `VEX_NO_ONBOARD=1` silences the
first-run offer (scripts/CI: flag commands never prompt — a missing
model there exits 4 with `run 'vex login'` as the fix).

Manual equivalent (what the wizard writes):

```bash
# point vex at your model once, then just use `vex`
vex config set base_url https://api.your-router.com/v1
vex config set model your-model-name
export VEX_API_KEY=...        # or: vex config set api_key ... (stored masked)

# or per-run flags:
vex fix --repo . --issue "..." --provider openai \
         --model your-model --api-key "$KEY" --api-base https://api.your-router.com/v1
```

No key and just want to see it work? The offline demo runs the real
loop (scripted model, real Docker sandbox + verifier + git output):

```bash
git clone https://github.com/Pavanteja2007/coding-harness && cd coding-harness
python demo/run_demo.py
```

> The distribution name is `vex-harness` (`vex`, `vex-cli`, `vexx`,
> `pyvex` were all taken on PyPI by unrelated packages); the installed
> command is `vex` — like `beautifulsoup4` installing as `bs4`.
> One-liner curl installers (pipx/venv, never your system Python) are
> in [Install](#install) below.

## What Vex can do

Typing `vex` with no arguments drops you into an interactive session —
type plain English; an offline intent router picks the right mode:

| you type | mode | what happens |
|---|---|---|
| "mean() in mathutil.py returns the sum, not the mean" | **fix** | the full verifier-gated pipeline (the default path) |
| "where is the retry loop handled?" | **question** | read-only Q&A over the codebase — retrieval + code graph + memory, no sandbox, no edits |
| "add a --json flag to the status command" | **build** | the agent writes acceptance tests for the feature FIRST, then builds until they pass — same verifier gate as fixes |
| "research the best approach for parsing RFC 3339 dates" | **research** | read-only FETCH-assisted synthesis from docs/web + repo context |
| "hi" / "what can you do?" | (conversation) | answered inline — no task is ever launched on ambiguous input |

Scriptable subcommands cover the same ground plus operations:

```
vex                        # interactive mode, or the subcommands below
vex fix --repo <path> --issue "<bug report>"        # one bug, one agent
vex fix --repo <path> --issue "<bug>" --json        # ...machine-readable result
vex run-benchmark --subset tasks.json               # N bugs, N supervised agents
vex status --task-id <id>                           # structured progress view
vex status --task-id <id> --json                    # ...as JSON for scripts
vex dashboard                                       # read-only web view of a run
vex --continue                                      # resume the last interrupted run
vex memory query-decisions "<topic>"                # the persistent memory layer
vex mcp call "<server cmd>" <tool> [--args '{..}']  # consume any external MCP server
vex mcp add <label> -- <cmd...>                     # named servers (global settings)
vex mcp list / health                               # ...merged view + per-server ok/fail
vex config set <key> <value>                        # global/project settings
vex login [--tier global|project]                  # configure a model (wizard)
vex logout                                         # remove the stored api_key
vex plugin install <dir-or-git-url>                 # skills/commands/tool bundles
vex plugin list / remove / enable / disable         # disable keeps it, skips it
vex skills list / show <name>                       # installed instruction packs
vex update [--check]                                # self-update (or just check)
vex completion <shell> [--install]                  # bash/zsh/fish/PowerShell
vex uninstall [--dry-run]                           # remove Vex completely
```

### Slash commands (interactive session)

Inside `vex`, every session command works in both the full-screen TUI
and the plain REPL (read-only ones also mid-run):

| command | what it does |
|---|---|
| `/help` | what you can say |
| `/status` | current/last task's structured state |
| `/diff` | re-render the last run's diff (`/diff undo [file\|all]` reverts agent edits) |
| `/sessions <query>` | search previous sessions (`status:`/`repo:`/`since:`/`resumable` + free text) |
| `/resume [<task_id>]` | continue an interrupted task (no id = most recent resumable) |
| `/plan [<text>]` | preview steps before edits (approve/reject) |
| `/review` | last fix's diff + rationale together |
| `/compact` | compact the conversation (recall-backed summary, recent kept) |
| `/copy-diff` | copy the last diff to the clipboard |
| `/history [text]` | search this session's input history (same filter grammar as `/sessions`) |
| `/init` | scaffold `.vex/` in the session repo (settings + example command/skill) |
| `/model [<name>]` | show the effective model (+ source), or pin `<name>` for later runs |
| `/login [global\|project]` · `/logout` | configure a model (wizard) · remove the stored `api_key` |
| `/mcp [label]` | list configured MCP servers (with a label: list that server's tools) |
| `/skills [filter]` | list discovered skills + origins |
| `/cost` | spend: last run + session total (trace usage-sum) |
| `/undo [file\|all]` | alias of `/diff undo` (agent sessions only) |
| `/clear` | fresh conversation (the old one is kept) |
| `/approve` · `/reject` | decide a pending approval request |
| `/cancel` | stop the current run cleanly (resumable) |
| `/quiet` | toggle live feed + spinner verbosity |
| `/trace` · `/feed` · `/steer` | live action feed · searchable feed history · steer the running task |
| `/<custom> [args]` | run a custom command from `.vex/commands/` (`$ARGUMENTS` = args) |

### Exit codes

Scripts and CI can rely on a stable numeric contract (also shown in
`vex --help`):

| code | meaning |
|---|---|
| 0 | success |
| 1 | task-level failure (the run didn't produce a verified fix) |
| 2 | usage or configuration error (bad arguments/files/config) |
| 3 | environment error (Docker down, missing dependency — fix before retrying) |
| 4 | model/network error (endpoint unreachable, auth rejected, rate limit) |
| 130 | interrupted by the user (Ctrl+C) |

The historic 0/1/2 contract is preserved exactly; 3 and 4 split what
used to be a catch-all 1 so pipelines can treat "fix the machine"
differently from "the bug beat the agent".

### `--json` output mode

`vex fix --json` prints one JSON document on stdout (status, attempts,
cost, verification flags, diff, trace path, exit-code reason — no
spinner, no theme); `vex status --task-id <id> --json` does the same
for the structured state view. Pair with the exit codes above and
scripts get both what happened and which category of failure it was.

### Plugins, skills & connectors

A **plugin** is a bundle of skills (SKILL.md instruction packs the
planner auto-invokes), slash-command templates, read-only tool verbs,
and MCP server references — installed by copy (self-contained,
removable; no registry/marketplace), e.g. the shipped example
`tests/fixtures/plugin-webapp-toolkit/` (a `django-style` skill, a
`/review` command, `ruff` BATCH verbs, a `structure-memory` MCP
reference):

```
vex plugin install tests/fixtures/plugin-webapp-toolkit
vex skills list                      # django-style (plugin) + description
vex plugin list                      # skills/commands/tools/mcp per plugin
vex mcp health                       # structure-memory ... ok (5 tools)
vex plugin disable webapp-toolkit    # dir stays; every scan skips it
vex plugin enable webapp-toolkit     # ...and it's back
```

A plugin's skill surfaces in the next plan automatically (origin
"plugin" in the `## Applicable skills` prompt section — no prompt
edits, no registration step). Disabling mutes its skills, commands,
tool verbs, and MCP servers together; `vex plugin list` still shows
the install honestly, marked `(disabled)`.

**Connectors** name the external MCP servers Vex consumes, in three
layers (later wins): global settings (`vex mcp add <label> --
<cmd...>` → the `[mcp_servers]` table in the global settings.toml),
project `<repo>/.vex/connectors.toml` (committable — no secrets), and
`<repo>/.vex/connectors.local.toml` (personal overrides, git-ignored).
`vex mcp list` shows the merged view with each label's source and its
launch command (secrets masked); `vex mcp health` spawns every server
and reports ok/fail per label, never a traceback. Configured labels
resolve anywhere a server goes (`vex mcp list-tools <label>`,
`vex mcp call <label> <tool>`, and the agent loop's `mcp` tool).

### Shell completions

```
vex completion bash        # (or zsh / fish / powershell) — print the script
vex completion --install   # write it to your shell's conventional location
```

The completions are dynamic (they ask `vex` itself for candidates), so
new subcommands and flags complete immediately after an upgrade.
Manual installation, if you prefer:

```bash
# bash:  vex completion bash > ~/.local/share/bash-completion/completions/vex
# zsh:   vex completion zsh > ~/.oh-my-zsh/completions/_vex   (or any fpath dir)
# fish:  vex completion fish > ~/.config/fish/completions/vex.fish
# PowerShell: add the output of `vex completion powershell` to $PROFILE
```

### Updating and uninstalling

```
vex update           # upgrade in place (pipx / installer-venv / pip — auto-detected)
vex update --check   # report installed vs latest release, no upgrade
vex uninstall        # remove Vex completely: venv, shims, PATH entry,
                     # config dirs — with confirmation (--dry-run previews)
```

`vex update` detects how you installed (the curl installers' dedicated
venv, pipx, or plain pip) and re-runs the matching upgrade from the
same source you installed with (PyPI by default, or your
`VEX_INSTALL_SOURCE` / `VEX_INSTALL_REPO` / `VEX_INSTALL_REF` git pin);
from a source checkout it prints the honest `git pull` +
`pip install -e .` recipe instead of pretending. Manual equivalents:

```bash
pipx upgrade vex-harness            # pipx installs
pip install --upgrade vex-harness  # PyPI installs
# or re-run any one-line installer (they upgrade in place)
```

Full manual removal (what `vex uninstall` automates): `pipx uninstall
vex-harness` or `pip uninstall vex-harness`, then delete the config
dir (`~/.config/vex` / `%APPDATA%\vex` / legacy `~/.vex`), the
installer venv (`~/.vex-venv`) and shim dir (`~/.vex/bin`) if present,
and the `~/.vex/bin` entry from your user PATH if the installer
added one.

Under the hood, per fix: the repo is snapshotted (the original is
never touched), a planner decomposes the fix into small verifiable
steps, an agent executes them with bash inside a locked-down Docker
sandbox (read-only rootfs, no network, resource limits, capability
drop), and **completion is verifier-gated** — a task only reports
`success` when the target test passes AND the full suite shows no
regressions (flake-checked, agent-written edge-case tests on top). On
verified fixes the harness additionally writes git-native output
(branch + commit + PR description), a human-readable rationale.md,
and a structured trace of every prompt/response/tool call.

## Install

Three one-liner installers (isolated — `pipx` if you have it,
otherwise a dedicated venv; never your system Python), or the plain
`pip install` above. All three install the latest `vex-harness` from
**PyPI** by default, verify with `vex --version`, and finish with
`vex update --check`:

```bash
# macOS, Linux, WSL:
curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.sh | bash

# Windows PowerShell:
irm https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.ps1 | iex

# Windows CMD:
curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.cmd -o install.cmd && install.cmd
```

Each checks Python 3.10+ (friendly instructions if missing), installs
Vex, puts `vex` on your PATH, and verifies it runs by printing the
installed version. Safe to re-run (upgrades in place). Requirements:
Python 3.10+, and — only for real bug-fixing — Docker plus a BYO
model endpoint/key (the offline demo needs neither; a missing Docker
or git is a warning, not an install failure).

Prefer your own package manager? The underlying step is just:

```bash
pipx install vex-harness            # isolated (recommended)
# or:
pip install vex-harness             # any venv
```

Need a git checkout instead (dev / mirror / pinned ref)? Set
`VEX_INSTALL_SOURCE` (any pip requirement, e.g.
`git+https://github.com/Pavanteja2007/coding-harness.git@main`) or
`VEX_INSTALL_REPO` / `VEX_INSTALL_REF` before running an installer —
`vex update` honors the same variables.

## The four layers

| Layer | What it is | Where |
|---|---|---|
| **Harness** | planner / step agent / verifier gate, repo snapshot + diff, git-native output, rationale log, resume contract | `harness/` ([architecture doc](docs/architecture-harness.md)) |
| **Execution** | Docker sandbox (fresh container per command, orphan reaping, serialized image builds), stateless verify + flake detection | `execution/` |
| **Runtime** | process-per-task scheduler (proven at 10–50 concurrent), checkpoint/resume across hard kills, approval gate, adaptive model router + per-call cost ledger | `runtime/` |
| **Memory + MCP** | tree-sitter code graph, SQLite decision memory, MCP server exposing 5 tools to any MCP client (Claude Code, Cursor, …), MCP client for consuming external servers | `memory/`, `mcp_server/` |

## The novel mechanism: adaptive model routing

Per call, the runtime predicts difficulty (intrinsic signal from the
issue text + struggle signal from the conversation tail — failing test
output, burned turns) and routes easy/medium calls to a cheap model,
hard calls to an expensive one. Every call lands in a per-task JSONL
ledger (model, tokens, cost, hint) — the mechanism is measurable, not
asserted.

**Ablation results (real bugs, real models, full real stack —
scheduler subprocesses → real harness → Docker verify):**

| run | arm | success | calls | tokens | cost* | wall |
|---|---|---|---|---|---|---|
| 5 fixture bugs | always-expensive | 5/5 | 17 | 38,680 | $0.0528 | 575s |
| 5 fixture bugs | adaptive | 5/5 | 31 | 69,615 | $0.0237 | 300s |
| 16-task expanded set | always-expensive | 16/16 | 81 | 138,526 | $0.1505 | 2717s |
| 16-task expanded set | adaptive | 16/16 | 71 | 136,436 | $0.0581 | 812s |
| 5 real OSS repos (Round 6) | always-expensive | 2/5 | 71 | 329,438 | $0.3059 | 2992s |
| 5 real OSS repos (Round 6) | adaptive | 3/5 | 75 | 302,801 | $0.0730 | 581s |

(Artifacts: `logs/ablations/v2-heuristic-*` (n=5) and
`logs/ablations/v4` (n=16; an earlier `v3-expanded` run is invalid —
fixture-path bug — superseded by `v3-expanded-fixed` and `v4`.)

- Same 100% success rate in every arm/run, at **45% (n=5) / 39%
  (n=16) of baseline cost** — same direction, growing margin with a
  more varied task set (16 tasks: 5 real fixture bugs + 11 synthesized
  repos with varied bug classes AND varied issue-text styles).
- Escalations did what they should: one genuine struggle escalation
  (cheap attempts failed → hard-tier call finished the task), zero
  cost-wasting escalations across the 8 easy-styled texts, and 1 of 2
  deliberately-SCARY texts (stack trace + "race" wording over a
  one-token bug) tricked the intrinsic scorer into one expensive call —
  the honest false-escalation data point (1/16 tasks).

\* Honesty notes: endpoints are free-tier BYO routers; token counts and
model-choice data are raw measurements from ledgers, costs use proxy
price rates for comparable model classes (both endpoints report no
cost) — the cost **delta** is a price-model delta, not a bill. n=5 and
n=16 × 1 rep are directional, not benchmark-grade. The Round-6
multi-repo arm additionally suffered endpoint degradation (2 timeouts in
the OFF arm were 0-call wall-clock kills, not model failures). Phase 6
should re-run on SWE-bench subsets with paid tiers. The full write-up
with all honesty notes: [RESULTS.md](RESULTS.md).

### Keeping the predictor honest: `vex analyze-history`

The routing difficulty predictor is not frozen — there is a maintenance
loop over accumulated real task logs:

```console
$ vex analyze-history            # scan logs/, aggregate, report
analyzed 294 real tasks (109 adaptively routed)
predictor divergence (routed tasks)
  aligned: 79   false escalations: 8   missed escalations: 22
...
difficulty predictor recalibration
  fitted bands: easy<=0 medium<5 hard>=5 (train bugs: 18)
  held-out before: acc=0.75 missed=1 false=0
  held-out after:  acc=0.75 missed=1 false=0
  recommendation: marginal - not worth applying (held-out delta zero)
```

It aggregates where predictions diverged from outcomes, which retrieval
strategies associate with more repair attempts, and common failure
patterns; then it refits the predictor's score bands on a training
split and compares against the ORIGINAL on held-out bugs. The
recalibration is **gated**: `--apply` writes the calibration file only
when the held-out comparison actually improved (the run shown above —
the real first run over this machine's accumulated logs — was honestly
*marginal*, so nothing was applied). Run it after any batch of real
runs accumulates, or before quoting routing numbers. Details:
`runtime/analyze_history.py` + `runtime/AGENTS.md`.

## Multi-repo validation: the system beyond its home turf

Beyond the fixture set, the full stack has been validated against real,
unfamiliar OSS code — not just the original repo:

- **5 real OSS repos through the routing ablation (Round 6)** —
  more-itertools, arrow, inflect, boltons, python-semver (pinned SHAs,
  one genuine introduced bug each, real suites, per-repo suite pins for
  dev-only deps): the adaptive arm went 3/5 (vs 2/5 always-expensive)
  at **24% of the cost and 5x faster wall** — first multi-repo evidence
  that the routing margin holds (and the failure modes are endpoint
  timeouts, not harness bugs; honest data point: multi-hundred-K-token
  real repos are simply harder than the fixture set, in BOTH arms).
  (`logs/ablations/v6-multirepo/`)
- **jaraco/path (full DoD)** — unfamiliar real OSS repo end-to-end:
  real cloud model, Docker sandbox, verifier-gated success in 1 attempt
  ($0.053, 6 calls), git-native branch/commit/PR, rationale.md,
  approval gate, memory ingestion — plus one honest first failure that
  exposed and fixed a real harness bug (binary-artifact diff crash).
  (`logs/oss-round4/`)
- **python-semver (module DoD)** — pristine baseline → broken-state
  detection → in-sandbox fix → verified, flake-flagging, git output,
  grounded rationale: 15/15 checks. (`logs/dod/`)
- **3 more real repos driven through the same stack (bottle, click,
  parse)** — full-stack runs with scripted models during a degraded
  endpoint window (real loop, real sandbox, real verify; the honest
  account is in `harness/AGENTS.md`). (`logs/oss-round6/`)

Net: 8 real OSS repos have been driven by the actual harness/verifier
stack, spanning plugin, date/time, inflection, and versioning domains
— with the same verifier-gated honesty rules as the fixture set:
nothing above is a claimed success without the gate.

## Runtime reliability (proven, not claimed)

- **45 tasks @ concurrency 45, 8 simultaneous mid-run hard kills**:
  45/45 success, 8/8 genuine resumes — verified from trace events
  (`plan_reused`, `step_skipped_resume`, pre-kill trace survival), zero
  leaked containers (`logs/stress/real-45/`).
- Concurrency cap proven from the event journal (max overlap ≤ cap);
  every kill recorded + requeued; parallel beats the serial floor.
- Scheduler + worker share one log tree (spawn pins `resume_dir` /
  `log_root`); a mid-run kill resumes from completed steps with all
  artifacts under the caller's `--log-root`.

## Memory layer (MCP)

- `query_structure` — tree-sitter code graph (functions, classes,
  calls, imports; persistent index)
- `query_decisions` / `record_decision` — decision/pattern memory
  (auto-ingests every task's structured state)
- `task_status`, `list_repos`

Run it: `python -m mcp_server` (stdio) and connect any MCP client. The
harness's retrieval consumes the same graph programmatically, so
structural context rides into prompts without re-reading files.

## Demo script (5 minutes)

Two variants: **zero-setup offline** (deterministic, no key/Docker) and
the real-model walkthrough.

```bash
# 0) The whole story, offline in one command (scripted model; the loop,
#    verifier gate, git output, rationale, and memory are all REAL):
python demo/run_demo.py        # fix -> git/PR -> routing numbers -> memory -> dashboard hint
#    per-step talking points: demo/README.md
```

With a real model (BYO endpoint/key):

```bash
# one-time: pip install vex-harness
#   (from a clone instead: pip install -e .  — or prefix every command
#    below with `python -m cli` instead of `vex`)

# 1) Fix a real bug with a real model (needs a BYO endpoint/key):
export MY_KEY=...          # your openai-compatible router key
vex fix \
  --repo cli/fixtures/smoke_repo \
  --issue "The mean() function in mathutil.py returns the sum instead of the arithmetic mean. Fix it so tests/test_mathutil.py::test_mean passes." \
  --provider openai --model <model> --api-key $MY_KEY --api-base <base-url>
# → status, cost, diff; then inspect the artifacts:
vex status --task-id <task_id>                     # plan checklist + decisions
type logs\<task_id>\rationale.md                   # what was wrong / what changed / why
type logs\<task_id>\git.json                       # branch + commit + PR description

# 2) Adaptive routing vs always-expensive, measured (the ablation):
#    endpoints/keys are configured in runtime/ablation.py (BYO, env keys)
python -m runtime.ablation --tasks all --concurrency 2   # both arms, one summary
type logs\ablations\<ts>\summary.json                    # per-arm cost/token table

# 3) Concurrency + crash-resume at target scale (offline, scripted model):
python -m runtime.stress --mode real --tasks 45 --concurrency 45 --kill 8

# 4) The memory layer, queried over MCP (our own server, external client style):
vex mcp call "python -m mcp_server" query_decisions --args "{\"query\": \"pytest\"}"
vex mcp list-tools "python -m mcp_server"

# 5) Read-only dashboard over any run's logs:
vex dashboard --logs-dir logs/ablations/<ts>/tasklogs
```

## Repo layout

```
harness/      agent loop: planner, steps, verifier gate, resume, git output
execution/    Docker sandbox + verify + git-native output + rationale
runtime/      scheduler, worker, checkpoint, router (novel mechanism), ablation
memory/       code graph (tree-sitter), decision store (SQLite), MCP client
mcp_server/   MCP exposure of memory/status (stdio)
cli/          the `vex` command (fix / run-benchmark / status / mcp / dashboard)
dashboard/    read-only web view of existing logs
demo/         one-command offline demo + walkthrough (run_demo.py)
tests/        ~300 tests incl. real e2e bug-fix runs and real process-kill resumes
logs/         (gitignored) per-task state, traces, ledgers, run journals
```

## Status & verification

- CI on every push, four workflow files (kept separate — the four
  modules were built in parallel terminals): `ci.yml` (runtime
  suites, nightly full stress + adversarial abuse), `harness-ci.yml`,
  `execution-ci.yml`, and `memory-cli-ci.yml` (memory/MCP incl. real
  stdio round-trip, CLI offline e2e through the real Docker sandbox,
  dashboard) — across Linux/Windows/macOS × Python 3.10/3.12.
  Docker-gated e2e tests self-skip with an explicit reason on runners
  without Docker. Badges at the top.
- Full test suite green (scheduler integration with real process
  kills, router, memory, MCP incl. real stdio round-trip, dashboard).
- **Adversarially tested (Round 6)**: the MCP server and CLI were
  probed with crafted/hostile inputs — path traversal, shell-injection
  payloads, SQL injection, malformed subsets, null bytes. One real
  data leak (task-id path traversal in `task_status`/`harness status`)
  was found live, fixed, and pinned by 101 adversarial tests; all other
  surfaces held (per-probe outcomes in each module's AGENTS.md; the
  Docker sandbox was adversarially confirmed separately — 24/24
  sequential + concurrent attack suites).
- Contract between modules: `INTERFACES.md`. Module-by-module state
  (what's built, what's stubbed, decisions): each module's
  `AGENTS.md`. High-level build history: `CHANGELOG.md` (current
  release: **v0.2.0**).
- Deferred per spec: SWE-bench Lite numbers (Phase 6), multi-language,
  plugin marketplace.
- PyPI: `vex-harness`; the installed command is `vex`.
- Deferred per spec: SWE-bench Lite numbers (Phase 6), multi-language,
  plugin marketplace.

## Tech

Python 3.10 · litellm (multi-provider, BYO-key) · Docker · tree-sitter
· MCP (official Python SDK) · argparse CLI · stdlib HTTP dashboard.
`litellm` is a real-model dependency (in `pyproject.toml`) pinned to
`1.74.9` on Python 3.10 (newer breaks the `typing` import on 3.10);
the offline/demo paths work without it (lazy import).

## Links

- **[Project website & docs](https://github.com/Pavanteja2007/coding-harness/tree/main/site)** — the marketing/docs site (Next.js, in `site/`)
- **[Bug reports & feature requests](https://github.com/Pavanteja2007/coding-harness/issues)** — GitHub Issues
- **[RESULTS.md](RESULTS.md)** — the full write-up on adaptive model routing (all ablation numbers + honesty notes)
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup, module map, boundary rules, lint/test workflow
- **[SECURITY.md](SECURITY.md)** — what's been adversarially tested, how to report a vulnerability
- **[CHANGELOG.md](CHANGELOG.md)** — milestone history
- **[PyPI: vex-harness](https://pypi.org/project/vex-harness/)** — releases
