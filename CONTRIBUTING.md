# Contributing to Vex

This guide is for anyone **extending or maintaining Vex itself** —
adding a tool to the agent's toolbox, wiring in a new model provider,
or changing one of the four modules. If you just want to *use* Vex,
the [README](README.md) is the place to start.

Vex is one system with four deliberately separated modules, built in
parallel by four workstreams. The rules below exist so that a change
made by someone who has never read the code still lands in the right
place, with the right tests, without breaking the other three modules.

---

## 0. Dev environment in one command

```bash
./scripts/dev-setup.sh        # or:  make dev
```

That installs dependencies (`pip install -e ".[dev]"`), installs the
git pre-commit hook (auto-format + lint on every commit), verifies
Docker is reachable (the sandbox needs it), and smoke-tests the CLI.
Safe to re-run. On Windows, run it from Git Bash (`bash` on PATH) or
use `make dev`.

Day-to-day:

| action | command |
|---|---|
| run the tests | `make test` (or `python -m pytest`) |
| auto-format your changes | `make fmt` (`ruff format .`) |
| lint (ratchet-aware) | `make lint` (see "Lint & format" below) |
| one offline end-to-end demo | `python demo/run_demo.py` |

---

## 1. The lay of the land (which module owns what)

```
harness/      the agent loop: planner, step sessions, verifier gate,
              resume, git-native output. Terminal 1.
execution/    Docker sandbox + verify + git output + rationale. Terminal 2.
runtime/      scheduler, worker processes, checkpointing, model router
              (the adaptive-routing mechanism). Terminal 3.
memory/       tree-sitter code graph + SQLite decision store. Terminal 4.
mcp_server/   MCP exposure of memory/status over stdio. Terminal 4.
cli/          the `vex` command — the only user-facing surface. Terminal 4.
dashboard/    read-only web view over existing logs.
shared/       dataclasses every module imports (Task, TaskResult, ...).
tests/        the whole test suite (~300 tests, incl. real e2e runs).
```

Per-module state (what's built, decisions, known issues) lives in each
module's `AGENTS.md` — read the one for the module you're touching
before you start. `INTERFACES.md` is the contract between modules.

## 2. The module-boundary rules (read before writing code)

These are the rules the four parallel workstreams were built under —
they are what keeps the system integrable:

1. **Build against `INTERFACES.md` contracts, not internals.** If
   module A needs something from module B, the call goes through a
   signature documented in `INTERFACES.md`. If what you need isn't
   there, ADD it there first (with a Change Log entry) — then code
   against it.
2. **Never reach into another module's internals.** If you find
   yourself importing `othermodule._private`, stop: either the
   interaction belongs in `INTERFACES.md`, or your feature belongs in
   that module.
3. **Stubs are only for dependencies that don't exist yet** (and are
   owned by another terminal/module). Never stub out your own module's
   responsibility. `harness/_stubs/` and `cli/_stubs/` exist as
   fallbacks when the real module is absent — they mirror the contract
   signature exactly, so the swap is invisible.
4. **Config values go through `Task.config`, never hardcoded
   constants.** Retry limits, budgets, model names, caps — everything
   that affects reproducibility rides on the task's config dict (merged
   over defaults in `harness/config.py` / `runtime/config.py`). This is
   spec item 10 and it makes every run reproducible + auditable.
5. **Every task run produces `logs/{task_id}/`** (state.json,
   trace.jsonl, and module-specific artifacts). Never silently swallow
   a failure — log it to the trace.
6. **Verifier-gated completion is absolute.** Nothing may set
   `status="success"` unless `verify()` confirms target-test pass + no
   regressions. Any new mechanism that could mint a success must go
   through (or sit before) that gate, and must never weaken it.
7. **The original repo is never mutated.** All agent work happens in
   `logs/{task_id}/work/` (a snapshot); diffs are computed against
   `pristine/`. Any new tool or feature must preserve this guarantee.

When you finish a chunk of work, update the Change Log in
`INTERFACES.md` if you changed any contract, and your module's
`AGENTS.md` so the other workstreams stay in sync.

## 3. How to add a tool to the agent

The agent's action space is deliberately minimal: **bash only, one
command per turn** (`harness/tools.py`, mini-swe-agent style — no
structured tool-calling needed, works with any model). "Adding a tool"
therefore means one of two things:

### A. A new control signal (like RECALL / DOCS / SUBMIT)

Control signals are intercepted harness-side *before* the reply is run
as a shell command. The recent examples to copy from:

- `harness/tools.py::parse_recall` (RECALL — on-demand reinjection of
  compacted trace context) — the oldest pattern: a regex on the model's
  reply, parsed in `run_step` on both the raw AND fence-stripped form
  (a fenced signal must never execute as shell), handled by the
  harness, answered by re-injecting a user message into the session.
- `harness/docs_lookup.py` (DOCS — docs/API lookup) — same skeleton,
  plus a config-gated budget (`max_docs_per_step`) and an audit trace
  event (`docs_lookup`) — read this one for the budgeted variant.

The recipe:

1. Add the parser (`parse_<signal>(text) -> Optional[str]`) in
   `harness/tools.py` or a dedicated module, mirroring the RECALL/DOCS
   pattern. It must NEVER execute the signal as a shell command.
2. Wire it in `harness/core.py::run_step`'s turn loop, checked on the
   raw reply and its fence-stripped form (see how RECALL is checked).
3. Budget it via config keys in `harness/config.py` defaults
   (`max_<signal>_per_step`, result caps) — budget exhaustion must
   nudge the model back to bash, never deadlock the step.
4. Log a trace event per use (the dashboards and the `vex status`
   enrichment key off trace events).
5. Document it in the STEP system prompt (`harness/prompts.py`) so the
   model knows it exists.
6. Tests: unit tests for the parser + an e2e test proving the
   re-injection actually reaches the session (see
   `tests/test_recall_unit.py` for the pattern).

### B. A new runtime capability the harness can use

If the "tool" is really a new harness-side capability (like the lint
gate, `harness/lint.py`, or structured tool-error classification,
`harness/tool_errors.py`), the pattern is:

1. New module under the owning module's directory, with a module
   docstring stating: what problem it fixes, its contract (never
   raises / degrades how?), and the config keys it reads.
2. Wire it at ONE point in the loop where the semantics live (e.g.
   the lint gate short-circuits `run_step` before a verify cycle; tool
   error classification wraps `BashSession._map_result`).
3. The new capability must not change the verifier-gated outcome —
   it can short-circuit known-broken states, never mint success.
4. Regression tests for the classes/shapes it handles + the degrade
   path (a crashed helper must never kill a run).

### What NOT to do

- Don't give the agent non-bash tools (structured tool calling) — that
  is a locked design decision (spec item 2); the bash-only loop is
  what works with any model.
- Don't add a tool whose failure can end a step with an exception —
  classify and feed back (`tool_errors.py`), or degrade loudly to a
  trace event.

## 4. How to add a model provider

Model access is locked to **litellm** (`runtime/model_router.py`),
which normalizes providers — so "adding a provider" is usually just
*configuration*, not code:

1. **Through config (the normal case):** litellm already supports your
   provider. Set it per run via the CLI flags or task config:
   `--provider openai --model <name> --api-key <key>` (or an
   openai-compatible router with `--api-base`). Any
   `openai/`-prefixed model string that litellm accepts works —
   nothing to write.
2. **Per-tier endpoints:** if you want different endpoints for the
   cheap/expensive routing tiers, `model_tiers` entries in task config
   accept their own `api_key` and `api_base` (documented in
   `runtime/config.py`).
3. **Only if litellm needs code:** a genuinely new provider class
   goes inside litellm (upstream), not in Vex. If you must bridge
   something litellm can't express, the seam is
   `runtime/model_router.call_model` — keep the Boundary-2 signature
   EXACTLY as documented in `INTERFACES.md` (the harness and every test
   build against it), and log a Change Log entry.
4. **Adaptive routing** (the measured novel mechanism): if you touch
   `difficulty_hint` semantics, remember callers must treat unknown
   hints as None, and the difficulty estimator keys on the planner
   prompt's `## Issue` / `## Retrieved context` section headers — keep
   those stable (mirrored in `runtime/difficulty.py`).

Whatever you do: never hardcode a key. Keys ride `Task.config` or env;
`task_start` trace events redact them.

## 5. Lint & format (one linter, a ratchet for old debt)

The repo uses **ruff** for both linting and formatting — configured in
`pyproject.toml` (`[tool.ruff]`), reused by the pre-commit hook, `make
lint`, and CI. If any future in-sandbox agent lint tool is added, it
must reuse this same config rather than introducing a second linter.

```bash
ruff format .        # format everything (make fmt)
ruff check .         # raw lint pass
make lint            # the ratchet check (what CI + the hook enforce)
```

**The ratchet** (`scripts/lint_ratchet.py` +
`scripts/lint-baseline.txt`): this codebase predates the linter, so
~60 files carry small pre-existing lint counts. The rule:

- a file **not** in `scripts/lint-baseline.txt` must have **zero**
  violations;
- a file in it must not **gain** any (its baseline count is the cap).

So new code is fully clean, and the debt can only shrink: clean a
baselined file to zero and it drops out at the next
`python scripts/lint_ratchet.py --update-baseline` (deliberate
action — never run that just to make CI pass; that's what the
ratchet exists to prevent).

The pre-commit hook (`scripts/hooks/pre-commit`, installed by
dev-setup / `make dev`) runs `ruff format` + `ruff check` on your
staged files and the ratchet repo-wide, on every commit. Bypassing
with `--no-verify` is discouraged — CI runs the same checks.

## 6. Tests — what to run, what to add

```bash
make test                        # full suite
python -m pytest tests/test_cli.py -q     # one module's selection
```

Conventions that matter:

- **Docker-gated tests self-skip** when the daemon is unreachable
  (explicit reason string) — but on a machine WITH Docker they must
  run; never make a Docker test skip green-on-Docker.
- e2e tests drive the REAL loop: real bash, real Docker sandbox, real
  pytest runs, with a **scripted model** injected via
  `harness.deps.set_call_model` (see `tests/fake_model.py`) —
  deterministic, no network. Cross-process tests use the
  `HARNESS_SCRIPTED_MODEL` env hook.
- Every bug fix ships with the regression test that would have caught
  it. The repo's history is full of "found live, fixed,
  regression-tested" — keep that bar.
- Test fixtures that contain *deliberate* bugs (`tests/fixtures/`,
  `cli/fixtures/`) are test data, not code to clean up — the bug IS
  the point, and the linter excludes them for exactly that reason.

## 7. Commit conventions

- `[module] short description` — e.g. `[harness] add patch validation`,
  `[runtime] pin resume_dir at spawn`, `[cli] plain-language errors`.
- Never commit secrets (API keys ride env/config; check your diff).
- The hook formats your staged Python files — re-stage happens
  automatically; just be aware your committed content may differ
  cosmetically from your working copy.

## 8. Where deeper docs live

- `project-spec.md` — the full feature inventory + rationale (read
  before writing code; the "Do not skip anything" rule applies).
- `INTERFACES.md` — every cross-module contract + the Change Log
  where contract changes must be announced.
- `<module>/AGENTS.md` — per-module state, decisions, known issues.
- `docs/architecture-harness.md` — prose walkthrough of the core loop.
- `CHANGELOG.md` — release-level history.
