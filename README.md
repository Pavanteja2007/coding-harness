# Neo

[![CI](https://github.com/Pavanteja2007/coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/ci.yml)
[![harness](https://github.com/Pavanteja2007/coding-harness/actions/workflows/harness-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/harness-ci.yml)
[![execution](https://github.com/Pavanteja2007/coding-harness/actions/workflows/execution-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/execution-ci.yml)
[![memory + MCP + CLI](https://github.com/Pavanteja2007/coding-harness/actions/workflows/memory-cli-ci.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/memory-cli-ci.yml)
[![release gate](https://github.com/Pavanteja2007/coding-harness/actions/workflows/release-gate.yml/badge.svg)](https://github.com/Pavanteja2007/coding-harness/actions/workflows/release-gate.yml)
[![PyPI](https://img.shields.io/pypi/v/neo-agent-cli)](https://pypi.org/project/neo-agent-cli/)
[![Python](https://img.shields.io/pypi/pyversions/neo-agent-cli)](https://pypi.org/project/neo-agent-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-9c6acc.svg)](LICENSE)

**Neo is an open-source, CLI-first AI coding agent that fixes real
software bugs end-to-end — and refuses to report success until the
repo's own test suite proves it.** One system where a Docker-sandboxed
agent loop, a concurrent checkpointing runtime, a persistent
cross-agent memory layer (exposed via MCP), and **adaptive model
routing by predicted difficulty** are integrated deliberately — the
integration is the point, not any one piece.

**Daily-use guide:** [quickstart](docs/quickstart.md) · [providers](docs/providers.md) · [commands](docs/commands.md) · [workflows](docs/workflows.md) · [permissions](docs/permissions-and-sandbox.md) · [extensions](docs/extensions.md) · [sessions](docs/sessions-and-recovery.md) · [headless/SDK](docs/headless-and-sdk.md) · [troubleshooting](docs/troubleshooting.md) · [architecture/events](docs/architecture-and-events.md) · [feature matrix](docs/feature-matrix.md) · [dogfood evidence](docs/dogfood-report.md)

## Neo fixing a real bug, end to end

![Neo fixing mean() in a sandboxed repo copy](demo/neo-demo.gif)

The production path is the real product path: `neo fix` snapshots the repository (the original is never touched), plans independently checkable steps, edits inside the Docker sandbox, and mints success **only by the verifier** — target test passes AND the full suite shows no regressions — never by the model's own claim. Every verified run leaves a branch + commit + PR description, a grounded `rationale.md`, and a complete `trace.jsonl` of every prompt, command, and decision.

The checked-in demo is intentionally narrower: it uses a scripted model and an explicit local subprocess sandbox fallback so it is deterministic on a machine without Docker or a key. It exercises the real harness loop, verifier gate, git artifacts, decision memory, and code graph; it is not Docker or live-provider evidence. Reproduce it with `python demo/run_demo.py`.

For the complete current guide, start with [`docs/onboarding.md`](docs/onboarding.md). The source checkout is the 0.3.0 candidate; the public PyPI release can lag it.

## Version truth — read this before you install

| | |
|---|---|
| **Source checkout** | `0.3.0` — declared in `pyproject.toml` |
| **Last public release** | **`0.2.0` on PyPI** — the only version a stranger can install today |
| **GitHub Releases** | **None.** No version has ever been cut on GitHub. |

`pip install neo-agent-cli` gives you **0.2.0**, which does not contain
`agent_sdk`, `acp`, `integrations`, `recipes`, or `extensions`. For those, use
the source checkout. `neo capabilities` reports the mismatch directly — a line
like `neo 0.2.1 (docs describe 0.3.0)` means your installed distribution is
older than the docs you are reading.

Why not publish this as 0.2.1: the tree contains a **breaking default change**
(an unqualified agent run now resolves to the `daily` engine instead of
`legacy_agent`), and shipping that as a patch increment would tell every
existing user that nothing important changed.

> ### ⛔ 0.3.0 is prepared but **not ready to publish**
>
> A parallel gate ran both engines against the same 10 real bugs in 5 real
> open-source projects ([`docs/release-verdict.md`](docs/release-verdict.md)):
> the `daily` path — the new default — scored **0/10**; the `legacy_agent`
> path it replaces scored **10/10**. The kernel's security policy refuses to
> let the agent *read* the test that defines success, so it stops after one
> turn, every time. Fixing that is the release blocker.

**Nothing has been published, tagged, pushed, or uploaded.**

## Quickstart

The full first-run path is in [`docs/quickstart.md`](docs/quickstart.md). The short version is:

```bash
python -m pip install neo-agent-cli
neo --version
neo login
cd /path/to/a/repository
neo
```

On a TTY, bare `neo` opens the TUI or its REPL fallback; it is not a piped prompt protocol. In CI, use an explicit subcommand. Requirements are Python 3.10-3.12, Git, Docker for the real fix path, and a model endpoint for a real model run. The first interactive run creates missing `.neo/` project files without overwriting existing files.

```bash
neo fix --repo . --issue "describe the bug and the failing test" \
  --target-test tests/test_example.py::test_case
```

Without a key, run the explicitly deterministic demo instead:

```bash
python demo/run_demo.py
python demo/agent_demo.py
```

### Auth: official models vs routers

`neo login` serves both shapes with the same flow (a failed test
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
settings file (never the committable project file — `neo config set
api_key --tier project` is refused); the model (+ provider) goes
global too unless `neo login --tier project`. Keys are masked in
`neo config list/get` (`sk-...<last4> (set)`), settings files are
chmod 600 where the OS allows, and `NEO_NO_ONBOARD=1` silences the
first-run offer (scripts/CI: flag commands never prompt — a missing
model there exits 4 with `run 'neo login'` as the fix).

Manual equivalent (what the wizard writes):

```bash
# point neo at your model once, then just use `neo`
neo config set base_url https://api.your-router.com/v1
neo config set model your-model-name
export NEO_API_KEY=...        # or: neo config set api_key ... (stored masked)

# or per-run flags:
neo fix --repo . --issue "..." --provider openai \
         --model your-model --api-key "$KEY" --api-base https://api.your-router.com/v1
```

No key and just want to see the loop? The offline demo uses the real harness
loop with a scripted model and an explicit local subprocess sandbox fallback;
it also shows verifier, git-native output, rationale, memory, and graph steps:

```bash
git clone https://github.com/Pavanteja2007/coding-harness && cd coding-harness
python demo/run_demo.py
python demo/agent_demo.py
```

> The distribution name is `neo-agent-cli` (`neo`, `neo-cli`, `neox`,
> `pyvex` were all taken on PyPI by unrelated packages); the installed
> command is `neo` — like `beautifulsoup4` installing as `bs4`.
> One-liner curl installers (pipx/venv, never your system Python) are
> in [Install](#install) below.

## What Neo can do

There are two intentional daily-use paths:

- `neo fix` is the explicit verifier-gated path. It snapshots the repository, uses Docker for the model loop and verifier, and emits git-native output only after clean target, regression, and flake evidence.
- Bare `neo` is a live-repository session. It can answer, inspect, run local commands, and edit the current checkout. Without a declared target test or test command, its completion is `completed_unverified`, not a verified fix.

Inside a session, `/mode` selects the current product profile:

| mode | use |
|---|---|
| **plan** | decompose work without changing files |
| **build** | implement and verify a feature; effects ask for approval |
| **explore** | read-only investigation with bounded reference access |
| **review** | read-only change review |
| **debug** | run diagnostics without source writes |
| **ask** | read-only repository question |

The compatibility classifier still recognizes ordinary work-shaped sentences, but explicit modes are the most predictable workflow. See [`docs/workflows.md`](docs/workflows.md) and [`docs/commands.md`](docs/commands.md) for the complete surface.

There are two surfaces, and one product. **The interactive product surface is the slash commands inside a session** (start `neo` with no arguments, then `/help`). Everything below is the **automation surface** — one-shot agent work plus the commands a script, a CI job, or an editor needs. `neo --help` lists that surface and nothing else.

```text
# one-shot agent work
neo -p "<plain-language sentence>"
neo -                                   # same, with context on stdin

# any slash command, non-interactively, through the SAME dispatcher
neo run "/<command> [args]"

# the automation surface
neo fix --repo <path> --issue "<bug report>" --target-test <node>
neo fix --repo <path> --issue "<bug>" --json
neo run-benchmark --subset tasks.json --concurrency 4
neo --continue
neo scan --repo . --json
neo profile list
neo dashboard
neo analyze-history --json
neo doctor --json
neo support-bundle --out ./neo-support.zip
neo serve --port 0 --json
neo acp --print-config
neo capabilities --json
neo update --check
neo completion bash
neo uninstall --dry-run
```

A **script form** is a top-level command that duplicates a slash command. They stay callable — a CI job and a session refusal sentence both need them — and `neo --help` deliberately omits them from the top-level listing so the two vocabularies stop looking like peers:

```text
neo status --task-id <id>      # /status, /cost
neo config list                # /settings, /init, /theme
neo login   neo logout   neo connect   neo auth
neo plugin list                # /plugins
neo skills list                # /skills
neo mcp list                   # /mcp
neo watch <task-id>            # /watch
neo hooks list                 # /hooks
neo migrate                    # /migrate
neo worktree list              # /worktree
```

`neo run "/<command>"` prints the exact line to run whenever a session command refuses headlessly, so a script can ask the product where to go instead of hard-coding it.

The authoritative current command table, including every TUI/REPL slash command and mid-run behavior, is [`docs/commands.md`](docs/commands.md). Provider setup and precedence live in [`docs/providers.md`](docs/providers.md).

### Slash commands (interactive session)

The TUI and rich REPL share the same command registry. The complete current reference is [`docs/commands.md`](docs/commands.md); the most useful daily commands are:

```text
/help  /mode  /status  /diff  /files  /checkpoints  /diagnostics
/plan  /review  /compact  /history  /trace  /feed  /steer
/sessions  /resume  /approve  /reject  /cancel
/init  /model  /login  /logout  /mcp  /skills  /cost
/undo  /redo  /export  /share  /clear  /quiet
```

Read-only views generally work while a run is live. Mutation and configuration commands either queue or refuse rather than racing a worker. `/steer` changes a running task; `/cancel` preserves checkpoints. Custom project commands use `$ARGUMENTS` and cannot shadow built-ins.

### Exit codes

Scripts and CI can rely on a stable numeric contract (also shown in
`neo --help`):

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

`neo fix --json` prints one JSON document on stdout (status, attempts,
cost, verification flags, diff, trace path, exit-code reason — no
spinner, no theme); `neo status --task-id <id> --json` does the same
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
neo plugin install tests/fixtures/plugin-webapp-toolkit
neo skills list                      # django-style (plugin) + description
neo plugin list                      # skills/commands/tools/mcp per plugin
neo mcp health                       # structure-memory ... ok (5 tools)
neo plugin disable webapp-toolkit    # dir stays; every scan skips it
neo plugin enable webapp-toolkit     # ...and it's back
```

A plugin's skill surfaces in the next plan automatically (origin
"plugin" in the `## Applicable skills` prompt section — no prompt
edits, no registration step). Disabling mutes its skills, commands,
tool verbs, and MCP servers together; `neo plugin list` still shows
the install honestly, marked `(disabled)`.

**Connectors** name the external MCP servers Neo consumes, in three
layers (later wins): global settings (`neo mcp add <label> --
<cmd...>` → the `[mcp_servers]` table in the global settings.toml),
project `<repo>/.neo/connectors.toml` (committable — no secrets), and
`<repo>/.neo/connectors.local.toml` (personal overrides, git-ignored).
`neo mcp list` shows the merged view with each label's source and its
launch command (secrets masked); `neo mcp health` spawns every server
and reports ok/fail per label, never a traceback. Configured labels
resolve anywhere a server goes (`neo mcp list-tools <label>`,
`neo mcp call <label> <tool>`, and the agent loop's `mcp` tool).

### Shell completions

```
neo completion bash        # (or zsh / fish / powershell) — print the script
neo completion --install   # write it to your shell's conventional location
```

The completions are dynamic (they ask `neo` itself for candidates), so
new subcommands and flags complete immediately after an upgrade.
Manual installation, if you prefer:

```bash
# bash:  neo completion bash > ~/.local/share/bash-completion/completions/neo
# zsh:   neo completion zsh > ~/.oh-my-zsh/completions/_neo   (or any fpath dir)
# fish:  neo completion fish > ~/.config/fish/completions/neo.fish
# PowerShell: add the output of `neo completion powershell` to $PROFILE
```

### Updating and uninstalling

```
neo update           # upgrade in place (pipx / installer-venv / pip — auto-detected)
neo update --check   # report installed vs latest release, no upgrade
neo uninstall        # remove Neo completely: venv, shims, PATH entry,
                     # config dirs — with confirmation (--dry-run previews)
```

`neo update` detects how you installed (the curl installers' dedicated
venv, pipx, or plain pip) and re-runs the matching upgrade from the
same source you installed with (PyPI by default, or your
`NEO_INSTALL_SOURCE` / `NEO_INSTALL_REPO` / `NEO_INSTALL_REF` git pin);
from a source checkout it prints the honest `git pull` +
`pip install -e .` recipe instead of pretending. Manual equivalents:

```bash
pipx upgrade neo-agent-cli            # pipx installs
pip install --upgrade neo-agent-cli  # PyPI installs
# or re-run any one-line installer (they upgrade in place)
```

Full manual removal (what `neo uninstall` automates): `pipx uninstall
neo-agent-cli` or `pip uninstall neo-agent-cli`, then delete the config
dir (`~/.config/neo` / `%APPDATA%\neo` / legacy `~/.neo`), the
installer venv (`~/.neo-venv`) and shim dir (`~/.neo/bin`) if present,
and the `~/.neo/bin` entry from your user PATH if the installer
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
`pip install` above. All three install the latest `neo-agent-cli` from
**PyPI** by default, verify with `neo --version`, and finish with
`neo update --check`:

```bash
# macOS, Linux, WSL:
curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.sh | bash

# Windows PowerShell:
irm https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.ps1 | iex

# Windows CMD:
curl -fsSL https://raw.githubusercontent.com/Pavanteja2007/coding-harness/main/install.cmd -o install.cmd && install.cmd
```

Each checks Python 3.10-3.12 (friendly instructions if missing), installs
Neo, puts `neo` on your PATH, and verifies it runs by printing the
installed version. Safe to re-run (upgrades in place). Requirements:
Python 3.10-3.12, and — only for real bug-fixing — Docker plus a BYO
model endpoint/key (the offline demo needs neither; a missing Docker
or git is a warning, not an install failure).

Prefer your own package manager? The underlying step is just:

```bash
pipx install neo-agent-cli            # isolated (recommended)
# or:
pip install neo-agent-cli             # any venv
```

Need a git checkout instead (dev / mirror / pinned ref)? Set
`NEO_INSTALL_SOURCE` (any pip requirement, e.g.
`git+https://github.com/Pavanteja2007/coding-harness.git@main`) or
`NEO_INSTALL_REPO` / `NEO_INSTALL_REF` before running an installer —
`neo update` honors the same variables.

### Release verification

Supported release lanes are CPython 3.10, 3.11, and 3.12 on Linux,
macOS, and Windows. `uv.lock` is the reviewed universal Python lock;
release CI installs with `uv sync --locked --all-extras`, while the
build backend itself is pinned to `setuptools==84.0.0`. Runtime
dependency ranges remain normal library policy, but CI and release-tool
versions are exact and lockfile hashes are checked before any build.

The clean-room matrix tests the exact wheel and sdist in fresh venvs. It
checks both console entry points and PATH resolution, configured
package imports, `pip check`, help/version, explicit login/logout,
update metadata, a packaged smoke benchmark, a real Docker-backed fix,
trace and git-native output evidence, disposable uninstall, and
source/package/original fixture integrity. Pip config/cache and
credential environments are isolated. Docker-dependent failures are
reported as `blocked`, never as passes.

From a clean, reviewed `v0.3.0` tag, prepare fresh output directories and
build twice with the commit timestamp:

```bash
python -m pip install uv==0.11.14
uv sync --locked --all-extras
. .venv/bin/activate
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
python -m build --outdir release/0.3.0/a
python -m build --outdir release/0.3.0/b
python -B scripts/verify_release.py \
  --project-root . --dist release/0.3.0/a \
  --compare-dist release/0.3.0/b \
  --source-date-epoch "$SOURCE_DATE_EPOCH" --normalize-sdist \
  --ignore-path release --require-clean --require-tag v0.3.0 \
  --report release/0.3.0/release-verification.json \
  --checksums release/0.3.0/SHA256SUMS
python -m twine check \
  release/0.3.0/a/neo_agent_cli-0.3.0-py3-none-any.whl \
  release/0.3.0/a/neo_agent_cli-0.3.0.tar.gz
uv export --locked --no-dev --no-emit-project --no-hashes \
  --format requirements-txt --output-file release/0.3.0/requirements.txt
cyclonedx-py requirements release/0.3.0/requirements.txt \
  --pyproject pyproject.toml --output-reproducible --output-format JSON \
  --output-file release/0.3.0/neo-agent-cli.cdx.json
```

Then run isolated installs with the exact artifacts:

```bash
python -B scripts/clean_room_matrix.py \
  --python 3.10=/path/to/python3.10 \
  --python 3.11=/path/to/python3.11 \
  --python 3.12=/path/to/python3.12 \
  --wheel /path/to/neo_agent_cli-0.3.0-py3-none-any.whl \
  --sdist /path/to/neo_agent_cli-0.3.0.tar.gz \
  --output-root /path/to/new-empty-release-matrix \
  --report /path/to/clean-room-matrix.json
```

The release scripts have machine-readable modes and stable exits: `0`
pass, `2` usage/verification failure, and `3` environment/internal
failure. Add `--events` to `verify_release.py` and
`clean_room_matrix.py`, or `--format ndjson` to
`scripts/github_workflow.py`, for one-JSON-event-per-line output.

GitHub issue and PR workflows are read-only and accept no mutation
verbs. They validate `owner/repo` plus a positive number, use fixed `gh`
argv without a shell, redact common credentials, and never approve,
comment, merge, push, or upload:

```bash
python -B scripts/github_workflow.py --report issue-task.json issue \
  --repo owner/repo --number 42 --repo-path /path/to/repo \
  --target-test tests/test_example.py
python -B scripts/github_workflow.py --format ndjson \
  --report pull-request-review.json review \
  --repo owner/repo --number 43 --repo-path /path/to/repo
```

The installers expose `NEO_FORCE_VENV=1` for deterministic venv-only
testing, `NEO_SKIP_UPDATE_CHECK=1` for offline install lanes, and
`NEO_REQUIRE_UPDATE_CHECK=1` when an operator explicitly wants a
network update check to fail installation. The default update check is
best-effort only after local version and command resolution pass.

After human review of the exact report, checksums, and SBOM, publication
is owner-only and uses named files—never a wildcard:

```bash
python -m twine upload \
  release/0.3.0/a/neo_agent_cli-0.3.0-py3-none-any.whl \
  release/0.3.0/a/neo_agent_cli-0.3.0.tar.gz
```

The current public PyPI release remains **0.2.0** until the owner publishes
this reviewed 0.3.0 candidate. Follow
[`docs/release-runbook.md`](docs/release-runbook.md) for the full procedure and
its preconditions.

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

### Keeping the predictor honest: `neo analyze-history`

The routing difficulty predictor is not frozen — there is a maintenance
loop over accumulated real task logs:

```console
$ neo analyze-history            # scan logs/, aggregate, report
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

## Demo scripts (5 minutes)

Two deterministic walkthroughs ship with the checkout:

```bash
# Real harness loop with a scripted model and explicit local sandbox fallback
python demo/run_demo.py        # fix -> git/PR -> routing -> memory -> graph -> dashboard

# Live-agent interaction demo: question -> plan -> approval -> edit -> undo -> resume -> compact
python demo/agent_demo.py
```

Both isolate generated state under `demo/demo-work*`, need no key or Docker,
and are deterministic. They are product-loop demonstrations, not live-provider
quality evidence. See [`demo/README.md`](demo/README.md) for the full transcript
and talking points.

With a real model and a real Docker daemon:

```bash
neo fix --repo cli/fixtures/smoke_repo \
  --issue "The mean() function in mathutil.py returns the sum instead of the arithmetic mean. Fix it so tests/test_mathutil.py::test_mean passes." \
  --target-test tests/test_mathutil.py::test_mean \
  --provider openai --model <model> --api-key "$MY_KEY" --api-base <base-url>

neo status --task-id <task-id> --json
cat logs/<task-id>/rationale.md
cat logs/<task-id>/git.json
```

For the current demo transcript, extension examples, and evidence labels, use
[`docs/dogfood-report.md`](docs/dogfood-report.md) and
[`demo/README.md`](demo/README.md).

## Repo layout

```
harness/      agent loop: planner, steps, verifier gate, resume, git output
execution/    Docker sandbox + verify + git-native output + rationale
runtime/      scheduler, worker, checkpoint, router (novel mechanism), ablation
memory/       code graph (tree-sitter), decision store (SQLite), MCP client
mcp_server/   MCP exposure of memory/status (stdio)
cli/          the `neo` command (fix / run-benchmark / status / mcp / dashboard)
dashboard/    read-only web view of existing logs
demo/         deterministic fix and agent walkthroughs
              (run_demo.py, agent_demo.py)
tests/        product, security, real e2e, and process-kill coverage
logs/         (gitignored) per-task state, traces, ledgers, run journals
```

## Status & verification

- CI on every push, five workflow files (four module suites plus the
  release gate): `ci.yml` (runtime
  suites, nightly full stress + adversarial abuse), `harness-ci.yml`,
  `execution-ci.yml`, `memory-cli-ci.yml` (memory/MCP incl. real
  stdio round-trip, CLI offline e2e through the real Docker sandbox,
  dashboard), and `release-gate.yml` (artifacts, installed wheel, full
  prompt matrix, and the Next.js site build/content/bundle gates) — module
  suites run across Linux/Windows/macOS ×
  Python 3.10/3.12; the release gate runs on Linux/Python 3.12.
  Docker-gated e2e tests self-skip with an explicit reason on runners
  without Docker. Badges at the top.
- The current source tree is shared and dirty, so this document does not
  claim that every repository-wide test, lint ratchet, release gate, and
  daily-driver readiness check is green. Use the focused commands and
  machine-readable reports named in the [dogfood evidence](docs/dogfood-report.md)
  and [handoff](logs/architecture-round/terminal-12.json).
- **Adversarially tested (Round 6)**: the MCP server and CLI were
  probed with crafted/hostile inputs — path traversal, shell-injection
  payloads, SQL injection, malformed subsets, null bytes. One real
  data leak (task-id path traversal in `task_status`/`harness status`)
  was found live, fixed, and pinned by the adversarial suites; all other
  recorded surfaces held. The Docker sandbox has separate adversarial
  evidence in `execution/AGENTS.md`.
- Contract between modules: `INTERFACES.md`. Module-by-module state
  (what's built, what's stubbed, decisions): each module's
  `AGENTS.md`. High-level build history: `CHANGELOG.md` (source candidate:
  **v0.3.0**; public PyPI remains **v0.2.0**, and no GitHub release has ever
  been cut, until an owner publishes the verified artifact).
- **Honest benchmark:** [`docs/benchmark.md`](docs/benchmark.md) — 14 tasks ×
  8 arms CLEAN through real Docker, **but 40–60% success on real
  third-party repositories**, not 100%. No SWE-bench number is claimed, no
  live-provider evidence exists, and every task set is a single repetition
  with no confidence intervals. The failures are in the report.
- **Known issues:** [`docs/known-issues.md`](docs/known-issues.md) — open
  blockers with reproducers, including two that ship on the new `daily`
  default path.
- Deferred or explicitly limited: SWE-bench Lite numbers, complete
  live-provider/manual-repair readiness, full LSP
  repair integration, and a plugin marketplace. Current limitations are
  listed in [`docs/feature-matrix.md`](docs/feature-matrix.md).
- PyPI: `neo-agent-cli`; the installed command is `neo`.

## Tech

Python 3.10-3.12 · litellm (multi-provider, BYO-key) · Docker · tree-sitter
· MCP (official Python SDK) · argparse CLI · stdlib HTTP dashboard.
`litellm` is a real-model dependency (in `pyproject.toml`) pinned to
`1.74.9` on Python 3.10 (newer breaks the `typing` import on 3.10);
the offline/demo paths work without it (lazy import).

## Links

- **[⛔ Release verdict](docs/release-verdict.md) — READ FIRST** — the `daily` default scores 0/10 on real bugs vs 10/10 for legacy. Verdict: **BLOCKED, do not cut over yet.**
- **[Onboarding](docs/onboarding.md)** — install to first verified fix, with a checkable success condition at every step
- **[Benchmark](docs/benchmark.md)** — every measured result with its methodology, caveats, and failures
- **[Release evidence](docs/release-evidence.md)** — which verification lanes ran, which were blocked, which were not run
- **[Known issues](docs/known-issues.md)** — the bug corpus, with reproducers
- **[Accessibility](docs/accessibility.md)** — measured terminal accessibility, verified on a real attached PTY
- **[Project website & docs](https://github.com/Pavanteja2007/coding-harness/tree/main/site)** — the marketing/docs site (Next.js, in `site/`)
- **[Bug reports & feature requests](https://github.com/Pavanteja2007/coding-harness/issues)** — GitHub Issues
- **[RESULTS.md](RESULTS.md)** — the full write-up on adaptive model routing (all ablation numbers + honesty notes)
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup, module map, boundary rules, lint/test workflow
- **[SECURITY.md](SECURITY.md)** — what's been adversarially tested, how to report a vulnerability
- **[CHANGELOG.md](CHANGELOG.md)** — milestone history
- **[PyPI: neo-agent-cli](https://pypi.org/project/neo-agent-cli/)** — releases (currently 0.2.0)
