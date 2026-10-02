# Command reference

This page reflects the current command registry and the current parser. The executable is `neo`; `harness` remains a compatibility alias.

## Two surfaces, and which one is the product

**The interactive product surface is the slash commands inside a session.** Start `neo` with no arguments and type `/help`; the TUI and the REPL read the same registry, so both show the same commands with the same arguments.

Everything below the "Top-level commands" table is the **automation surface**: one-shot agent work plus the commands a script, a CI job, or an editor needs. Any slash command runs non-interactively through the same implementation:

```bash
neo run "/help"
neo run "/trace 12"
neo run "/doctor" --json
```

`neo run` calls the same dispatcher a session does, so a receipt from a script and a receipt from a TTY agree on status, exit code, and verification. Exit codes are the same six on both surfaces.

## Top-level commands

This is the automation surface `neo --help` lists. A command that only duplicates a slash command is a *script form*: it stays callable (a CI job or a refusal sentence needs it) and is documented in "Script forms" below rather than presented as a peer of `/help`.

| Command | Use it for | Important options |
|---|---|---|
| `neo` | Interactive TUI or REPL in the current repository | TTY only; use `NEO_TUI=0` for the REPL fallback |
| `neo -p "<sentence>"` | One-shot agent work; prints a machine-readable envelope | `--repo`, `--log-root`, `--session-id`, `--json` |
| `neo -` | The same, with context piped on standard input | same flags; a usage error on an interactive terminal |
| `neo run "/<command>"` | Run any slash command non-interactively | `--repo`, `--log-root`, `--json`, `--at`, `--schedule-*` |
| `neo fix` | One verifier-gated task in one repository | `--repo`, `--issue` or `--finding`, `--target-test`, `--json`, `--approval`, `--adaptive-routing` |
| `neo run-benchmark` | A JSON subset or `smoke` through the scheduler | `--subset`, `--concurrency`, provider/config flags |
| `neo scan` | Read-only coverage, smell, and dependency analysis | `--focus`, `--remote`, `--max-findings`, `--json`, `--fix` |
| `neo profile` | Manage named provider profiles | `list`, `show`, `use`, `remove` |
| `neo memory` | Decision and structure memory utilities | `record`, `query-decisions`, `query-structure`, `ingest` |
| `neo analyze-history` | Offline routing/history analysis | `--json`, `--apply`, `--out`, `--holdout-frac` |
| `neo dashboard` | Read-only web view over existing logs | `--logs-dir`, `--host`, `--port`, `--no-browser` |
| `neo doctor` | Read-only health check with actionable remediation | `--repo`, `--log-root`, `--json` |
| `neo support-bundle` | Version, environment, config shape, and recent errors in one archive | `--out`, `--recent-runs`, `--no-archive`, `--json` |
| `neo serve` | The local agent server (loopback HTTP/SSE/WebSocket) | `--host`, `--port`, `--auth-token`, `--allow-non-loopback`, `--json` |
| `neo acp` | Agent Client Protocol v1 on stdio, for an editor | `--editor`, `--print-config`, `--no-first-run-notice` |
| `neo capabilities` | What this installation can actually do | `--json` |
| `neo update` | Check or apply the matching update method | `--check` |
| `neo completion` | Generate or install shell completion | `bash`, `zsh`, `fish`, `powershell` |
| `neo uninstall` | Remove Neo-owned installation and configuration | `--yes`, `--dry-run` |

### Script forms of slash commands

These stay dispatchable for CI, for editor integrations, and because a session command's refusal names the exact line to run instead. `neo <name> --help` documents each one, and `neo run "/<command>"` prints the same line when a session command refuses headlessly.

| Script form | The session door |
|---|---|
| `neo status --task-id <id>` | `/status`, and `/cost` |
| `neo config <subcommand>` | `/settings`, `/init`, `/theme` |
| `neo login` | `/login`, and `/model` |
| `neo logout` | `/logout` |
| `neo connect` | `/connect` |
| `neo auth` | `/login` and `/logout` (same credential store) |
| `neo mcp <subcommand>` | `/mcp` |
| `neo skills <subcommand>` | `/skills` |
| `neo plugin <subcommand>` | `/plugins` |
| `neo watch <task-id>` | `/watch` |
| `neo hooks <list\|run>` | `/hooks` |
| `neo migrate` | `/migrate` |
| `neo worktree <new\|list\|go\|rm>` | `/worktree` |

`neo --help` deliberately omits these from the top-level listing. They are not removed: the registry rows in `cli/commands.py` that tell a session user what to type still point at them.

## Fix and benchmark flags

The task commands share the model, provider, base URL, target test, retry, budget, approval, protected-path, adaptive-routing, and log-root options. Useful examples:

```bash
neo fix --repo ./repo --issue "..." --target-test tests/test_x.py::test_case --json
neo run-benchmark --subset tasks.json --concurrency 4
neo scan --repo ./repo --focus coverage --json
```

For a finding from a scan:

```bash
neo fix --repo . --finding scan-abc123#2
```

`--json` is a machine-readable document on stdout. Human spinners and theme output are suppressed, and provider errors are kept off the JSON document. Use the process exit code plus the JSON `status` and verification fields; do not infer success from the presence of a diff.

## Session flags

These work without a subcommand:

```bash
neo --list-sessions
neo --continue
neo --resume TASK_ID
```

They are scriptable and do not require a TTY. A session is resumable only when its durable state has the required progress and no terminal result.

## TUI and REPL slash commands

The TUI and rich REPL share the same command metadata. Read-only commands are generally safe while a run is active; mutation, undo, login, and export surfaces either queue or refuse with a clear message.

| Command | Behavior |
|---|---|
| `/help` | Show the interactive command guide |
| `/mode <name>` | Select `plan`, `build`, `explore`, `review`, `debug`, or `ask` |
| `/status` | Show current or last task state |
| `/diff` | Show the current diff; `/diff undo [file\|all]` restores agent edits |
| `/files` | Browse repository files |
| `/checkpoints` | Browse durable run checkpoints |
| `/redo` | Redo the last undone agent edit when its receipt is still valid |
| `/diagnostics` | Show available language-server diagnostics |
| `/export [path]` | Write a redacted local session artifact |
| `/share [path]` | Write a metadata-oriented shareable artifact |
| `/sessions [query]` | Search sessions; filters include `status:`, `repo:`, `since:`, and `resumable` |
| `/resume [id]` | Resume a resumable task; no id selects the most recent |
| `/approve` / `/reject` | Decide a pending approval request |
| `/cancel` | Cancel the current run cleanly while preserving checkpoints |
| `/quiet` | Toggle spinner and live-feed verbosity |
| `/plan [text]` | Preview a plan; text forces the preview for that run |
| `/compact` | Compact older conversation turns while keeping recent turns |
| `/copy-diff` | Copy the current diff when a clipboard is available |
| `/history [text]` | Search this session's input history |
| `/trace [n]` | Inspect or expand trace feed entries |
| `/feed [text]` | Browse/search the full action feed |
| `/steer <text>` | Guide, replan, or abort the running task |
| `/init` | Scaffold missing `.neo/` project files |
| `/model [name]` | Show the effective model/source or pin a model |
| `/effort [level]` | Show or set how hard the model thinks (`auto\|low\|medium\|high\|xhigh\|max`, alias `/thinking`). Reports which provider parameter was actually sent, or that the provider cannot honour the level. Also settable with `NEO_EFFORT` or `effort` in config. |
| `/login [tier]` / `/logout` | Configure or remove model credentials |
| `/mcp [label]` | List configured connectors or one connector's tools |
| `/skills [filter]` | List discovered skills and their origins |
| `/cost` | Show last-run and session spend from trace usage |
| `/undo [file\|all]` | Alias for agent diff undo |
| `/clear` | Start a new conversation while retaining the old snapshot |
| `/review` | Show the last fix's diff and rationale; a resolvable template may handle arguments |

Bare session words are also available: `repo <path>`, `model <name>`, `help`, and `exit`.

### Custom slash commands

Project commands live in `.neo/commands/<name>.md`, global commands in `~/.config/neo/commands/`, and plugin commands in the installed plugin's `commands/` directory. The template receives arguments through `$ARGUMENTS`:

```markdown
Review the authentication change and list concrete risks.

Focus: $ARGUMENTS
```

Project beats global, and global beats plugin on a name collision. Built-ins cannot be shadowed. `/review` is intentionally resolvable as a custom template when arguments are supplied, while its bare form is the built-in diff-and-rationale view.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | Task-level failure |
| 2 | Usage or configuration error |
| 3 | Environment error, such as unavailable Docker or dependencies |
| 4 | Model, endpoint, authentication, or network error |
| 130 | Interrupted by the user |

`run-benchmark` should be checked against its machine-readable task results as well as its process status; individual task outcomes remain visible in the run artifacts.
