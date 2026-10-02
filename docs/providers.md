# Providers and routing

Neo uses LiteLLM for provider access. A provider is a bring-your-own-key model plus, when needed, an OpenAI-compatible base URL.

## Configuration precedence

The effective value is resolved highest to lowest:

1. Explicit flags or interactive session state
2. Environment variables: `NEO_MODEL`, `NEO_PROVIDER`, `NEO_BASE_URL`, `NEO_API_BASE`, `NEO_API_KEY`, and the active profile variable
3. An active named provider profile
4. `<repo>/.neo/settings.local.toml`
5. `<repo>/.neo/settings.toml`
6. The global `settings.toml` (`%APPDATA%\neo\settings.toml` on Windows, `~/.config/neo/settings.toml` on POSIX; `$NEO_CONFIG` overrides it)
7. Legacy `~/.neo/config.toml`
8. Built-in defaults

Inspect the resolved values without printing the key:

```bash
neo config path
neo config list
neo config status --json
```

`api_key` cannot be written to the committable project settings file. Use the global file, a local settings file, or an environment variable. Configuration displays, connector displays, and error output are redacted, but operators should still avoid sharing raw environment dumps.

## First-time wizard

```bash
neo login
```

The wizard offers official OpenAI, Anthropic, and Gemini presets, router presets, Ollama, and a custom OpenAI-compatible endpoint. Remote credentials must pass a small health request before they are saved. A local endpoint such as Ollama can skip the remote health check.

For a named profile:

```bash
neo login --profile work
neo profile list
neo profile show work
neo profile use work --tier project
```

Profiles merge across settings tiers. A profile is a convenient provider selection, not a secret store that should be committed.

## Common environment setups

### OpenAI-compatible router

```bash
export NEO_PROVIDER=openai
export NEO_BASE_URL=https://api.example.com/v1
export NEO_MODEL=vendor/model-name
export NEO_API_KEY="$MY_KEY"
```

The model name is intentionally free-form. The endpoint and model are passed to LiteLLM; Neo does not require a closed list of routers.

### Local Ollama

```bash
export NEO_PROVIDER=openai
export NEO_BASE_URL=http://localhost:11434/v1
export NEO_MODEL=qwen2.5
```

Ollama is local and normally needs no key. A missing local service is an environment error, not a successful model run.

### Per-run override

```bash
neo fix --repo . --issue "..." \
  --provider openai \
  --model vendor/model-name \
  --api-base https://api.example.com/v1 \
  --api-key "$MY_KEY"
```

Prefer an environment variable over a command-line key because process arguments can be visible to other local tools.

## Adaptive routing

Adaptive routing predicts difficulty from the issue and live struggle signals, then chooses a model tier. Enable it for a run with:

```bash
neo fix --repo . --issue "..." --adaptive-routing
```

The runtime accepts a `model_tiers` mapping in `Task.config`. Each tier can carry its own `provider`, `model`, `api_key`, and `api_base`, so cheap and escalation tiers can use different gateways. Example shape:

```toml
adaptive_routing = true

[model_tiers.easy]
provider = "openai"
model = "fast-model"
base_url = "https://fast.example/v1"

[model_tiers.hard]
provider = "openai"
model = "strong-model"
base_url = "https://strong.example/v1"
```

The exact tier names and defaults are runtime configuration, not a promise that every provider has the same model catalog. The router records model, tier, tokens, latency, and cost in the per-task ledger. Use `neo analyze-history` to inspect accumulated routing evidence; `--apply` writes calibration only when its held-out comparison improves.

## Effort levels

`effort` controls how hard the model thinks. It changes nothing about whether a
result is verified — a completed run is `completed_unverified` at every level.

There is no `--effort` CLI flag. The three ways to set it are:

| surface | how | scope |
|---|---|---|
| config | `effort = "high"` in a `.neo/settings*.toml` tier, or `Task.config["effort"]` | that run |
| environment | `NEO_EFFORT=high` | this process and its children |
| in a session | `/effort high` (alias `/thinking`), mid-run allowed | the session, from the next model call |

Levels are `auto | low | medium | high | xhigh | max`. `auto` is the default
and **sends no effort parameter at all**, so an unconfigured run is byte-identical
to a run before the ladder existed.

Each level is mapped onto the provider's real parameter, and the ledger row
names which one was sent:

| model family | parameter | levels |
|---|---|---|
| OpenAI (`gpt-5`, `o`-series) | `reasoning_effort` | `low`, `medium`, `high` |
| Anthropic (`claude-*`) | `thinking` (`budget_tokens`) | all five |
| Google (`gemini-*`) | `thinking_budget` | all five |
| anything else | *none* | reported as `unsupported_model` |

Three things are deliberately **not** done, and each is a reported outcome
rather than a silent one:

- A level the family does not accept (`max` on a three-level provider) is
  **not clamped down** to the nearest rung it does accept. The run continues
  and the receipt says `unsupported_level`.
- A model with no declared knob is `unsupported_model`, not "set to high".
- An unrecognised value (`hig`) is `invalid`, with the value echoed, not
  rounded to the default.

`effort_parameter` overrides the knob choice: a string names the parameter to
send, and `none` (or `off`/`false`/`0`/`no`) sends nothing at all — the escape
hatch for an endpoint that rejects an unknown keyword. A bring-your-own gateway
can also register a knob through
`runtime.model_capabilities.register_effort_knob(EffortKnob(...))`; a
malformed declaration raises rather than defaulting.

Because the rung is part of the resume identity, resuming a run recorded at
`high` while configured at `low` starts a **fresh attempt** instead of quietly
continuing at a different level. Summarisation and compaction default to the
cheap tier (`context_compaction_tier = "cheap"`); set it to `expensive` to pay
frontier prices for paraphrasing dropped turns.

## Health and failure behavior

- Authentication, rate limit, timeout, and endpoint failures are model/network failures: exit `4`.
- Docker, dependency, or sandbox failures are environment failures: exit `3`.
- Configuration and argument mistakes are usage failures: exit `2`.
- A task can complete without a verifier as `completed_unverified`; that is not a verified success.

For a full table and recovery steps, see [Troubleshooting](troubleshooting.md).

## What is not claimed

The current checkout has provider routing and an opt-in live-provider eval lane, but a deterministic fixture is not a real-model quality result. The latest readiness report must be consulted for whether the live-provider lane actually ran. See [Dogfood evidence](dogfood-report.md).
