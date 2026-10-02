# Quickstart

This is the shortest safe path from a fresh checkout to a verified task.

## Requirements

- Python 3.10, 3.11, or 3.12
- Git for repository history
- Docker for the real `neo fix` sandbox and verifier
- A model endpoint and credential for a real model run

The offline demos do not need a key or Docker. The offline fix demo uses the local subprocess sandbox stub on purpose; it is deterministic, not a Docker claim.

## Install

For a released package:

```bash
python -m pip install neo-agent-cli
neo --version
```

For this checkout:

```bash
python -m pip install -e .
python -m cli --version
```

The checkout is the 0.2.1 source candidate. The public PyPI latest may still be 0.2.0, so compare `neo --version` with the release notes before relying on a source-only feature.

## First run in a repository

```bash
cd /path/to/a/repository
neo
```

On a TTY, Neo opens the Textual TUI when available and otherwise uses the rich REPL. On a non-TTY, no-argument invocation prints usage and exits 2; use an explicit subcommand in CI or scripts.

The first interactive run creates missing global settings and scaffolds the project layout without overwriting existing files:

```text
.neo/settings.toml
.neo/settings.local.toml
.neo/commands/fix.md
.neo/skills/code-review/SKILL.md
```

`settings.local.toml` is personal and should stay ignored. The project settings file is safe to commit only when it contains no credentials.

## Configure a model

The wizard checks the endpoint before saving a remote credential:

```bash
neo login
neo config status
```

For a non-interactive setup, prefer an environment variable:

```bash
export NEO_BASE_URL="https://your-router.example/v1"
export NEO_MODEL="your-model-name"
export NEO_PROVIDER="openai"
export NEO_API_KEY="your-secret"
neo config status --json
```

See [Providers and routing](providers.md) for profiles, precedence, local Ollama, and adaptive routing. Never put a key in a committed `.neo/settings.toml`.

## Run the first verified fix

Use a target test so the verifier has a precise contract:

```bash
neo fix --repo . \
  --issue "mean() returns the sum instead of the arithmetic mean" \
  --target-test tests/test_mathutil.py::test_mean
```

A verified result requires the target test, the full suite, and the non-flake check. Inspect the artifacts:

```bash
neo status --task-id <task-id>
cat logs/<task-id>/rationale.md
cat logs/<task-id>/git.json
python -m shared.traceview <task-id> --logs-root logs --summary
```

The original repository is not the fix workspace. Verified fixes are composed in the private `pristine/` and `work/` copies under the task log directory.

## Run without a model or Docker

From the repository root:

```bash
python demo/run_demo.py
python demo/agent_demo.py
```

The first command shows the verifier-gated fix, git-native artifacts, routing summary, decision memory, and code graph. The second shows question, `@file` context, plan preview, approval, edit, diff, undo, resume replay, and compaction. Both isolate their state under `demo/demo-work*` and use scripted models. Read [the demo guide](../demo/README.md) before presenting either as real-provider evidence.

## CI smoke check

```bash
python -m evals.run --suite prompt-regression --check --json
```

The host self-check validates the fixed task set without Docker. Use [Headless, CI, and SDK](headless-and-sdk.md) for JSON output and exit-code handling.
