# Extensions: hooks and plugin lifecycle

## Built surfaces

`extensions/__init__.py` re-exports the public hook and plugin lifecycle names for package-level imports.

`extensions/hooks.py` provides the public hook contract:

- `HookPoint` covers task-before/after, tool-before/after, permission-before/after, and completion-before/after. The `COMPLETION` alias targets completion-after.
- `HookContext` is the redacted callback input. It carries task/tool/permission/result data, metadata, arguments, payload, and bounded mutation state.
- `HookDecision`, `HookOutcome`, and `HookRecord` are typed return, aggregate, and per-registration audit values.
- `HookManager` supports registration, decorator registration, deterministic dispatch, unregister/remove-by-owner, audit history, and thread-safe snapshots.
- Typed hook errors cover registration, validation/payload, dispatch, and security failures.

`extensions/plugins.py` provides the lifecycle contract:

- `Plugin`, `PluginSpec`, `PluginManifest`, `PluginState`, and `PluginManager` cover typed registration and state transitions.
- Registration supports already-created plugins, injected factories, factory maps, declarative hooks, and explicit activation.
- `register`, `activate`, `deactivate`, `enable`, `disable`, `remove`, `reload`, `activate_all`, and `deactivate_all` are available.
- `load_plugin_file` and `load_manifest` load only an explicitly supplied, contained local file. The file loader requires a regular non-symlinked `.py` file below the supplied root and requires non-relative imports to be explicitly allowlisted with `allowed_imports`/`imports`.
- Typed plugin errors cover manifest/spec validation, registration, lookup, state, activation, deactivation, loading, and security failures.

No marketplace, registry service, installer, or network discovery is implemented.

## Isolation and security guarantees

- Registrations are snapshotted and ordered by descending priority, then ascending registration order.
- Every registered hook gets an audit row, including skipped rows after a deny or short circuit.
- Ordinary callback and validation exceptions are redacted, recorded, and isolated; later hooks still run.
- A hook security violation is recorded and raised as `HookSecurityError`; it is never downgraded to a normal warning.
- Before hooks may deny, short-circuit, merge bounded mutations, and return bounded payloads. Permission decisions are restricted to `allow`, `ask`, and `deny`, with restrictive precedence.
- After and completion callbacks are observational. Their decisions, mutations, payloads, and exceptions cannot alter the canonical result or status.
- Completion callbacks receive a redacted `RunResult` projection. The caller’s original result remains the authoritative outcome.
- Context, metadata, payloads, errors, records, status mappings, and repr output use `shared.security` redaction and bounded serialization.
- Plugin hook registrations are owner-scoped. Deactivation and removal remove all owned hooks; activation failure rolls back partial registrations.
- Factories are lazy and hydrated once per active plugin instance. Activation/deactivation callback failures are recorded in plugin state and metadata without stopping peer plugins by default.
- Lifecycle state transitions and error metadata are available through `Plugin.to_dict()`, `PluginManager.status()`, and the manager state/error methods.
- Manager and hook registry operations are thread safe. Hook dispatch uses a stable per-dispatch snapshot, deep-copies callback contexts, preserves the raw canonical result only in trusted in-process audit metadata, and shares owner locks with plugin deactivation.

## Tests and verification

`tests/test_extensions.py` covers all hook points, priority/registration ordering, before mutation/deny/short-circuit behavior, observational result protection, permission action validation, completion redaction, exception isolation, security denial propagation, plugin state transitions, one-time activation, factory hydration/reload, rollback, cleanup, manifest validation, local-file containment, symlink rejection where supported, duplicate registration, and concurrent registration/dispatch.

Verification run from the repository root:

```text
python -m pytest tests/test_extensions.py -q
19 passed, 1 platform skip
```

The single skip is the Windows symlink-privilege/platform case for the local-file containment regression; it is not counted as a pass.

```text
python -m ruff check extensions/__init__.py extensions/hooks.py extensions/plugins.py tests/test_extensions.py
All checks passed!

python -m ruff format --check extensions/__init__.py extensions/hooks.py extensions/plugins.py tests/test_extensions.py
4 files already formatted
```

No Docker, live provider, marketplace, or network-dependent test is required by this module.

## VEX-CEILING-12 — user hooks, versioned skill/agent policy (2026-09-26)

Two new modules. `extensions/hooks.py` above is the **in-process** contract a
Python plugin registers callbacks against; `extensions/user_hooks.py` is the
**declarative** layer an operator or a repository configures in JSON. They are
different surfaces on purpose and neither wraps the other.

### `extensions/user_hooks.py` — declarative lifecycle hooks

Seven events, exactly as the ceiling names them: `SessionStart`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `PreCompact`,
`SessionEnd`.

Three config files merge with **explicit, total** precedence `local` >
`project` > `user`:

| tier | file |
|---|---|
| user | `<global config root>/hooks.json` (`$NEO_HOOKS_DIR/hooks.json` overrides) |
| project | `<repo>/.neo/hooks.json` |
| local | `<repo>/.neo/hooks.local.json` |

A hook `id` that appears in more than one tier is **replaced** by the
higher-precedence tier, so a project can neutralize an inherited personal hook
by id. Registrations with distinct ids all run, ordered by
`(tier_rank, declared_order, id)` — the same order on every run, which is what
`test_required_1` pins. Every surviving registration keeps its tier label, so
the trace says which file decided it.

**`matcher` / `if` use the permission-rule form.** The six dimension names
(`tool`, `path`, `command_prefix`, `mcp_server`, `network_domain`,
`side_effect_class`) and the AND-within-a-block semantics are deliberately
identical to `harness.agent_kernel.policy.PolicyRule`, so an operator who has
already written permission rules writes a hook matcher in the same vocabulary.
`matcher` and `if` are AND-combined. `*` and an absent field both mean
"unconstrained". An unknown dimension name is a `HookConfigError`, never a
silently-ignored key.

**Handlers, in the order the prompt asks for them:**

- `command` — a fixed argv. A **shell string is refused**: a config that said
  `"command": "rm -rf /"` would make every hook an arbitrary code path. Runs
  with `shell=False`, a `shared.security.scrub_environment()` child, a
  repo-pinned cwd, a per-hook timeout, and stdout bounded to 8 KiB.
  Placeholders `{path} {tool} {task_id} {session_id} {event}` are substituted
  (safe, because the argv never reaches a shell) and a substituted value
  containing a NUL is refused rather than truncated.
- `http` — one bounded POST through `shared.egress.egress_decision`, so a hook
  **cannot** reach a host the operator did not allowlist. A denial returns
  status 403 and lands in `HookOutcome.failures` — a refusal is not a hook that
  ran and said nothing.
- `prompt` — OPTIONAL and additive only. It renders bounded
  `additional_context` and has no path to a decision. With no injected
  `prompt_renderer` the hook is **skipped and the skip is recorded**, never
  silently dropped.

**Hook output is bounded, typed, and cannot silently mutate policy.** The
accepted keys are `decision`/`continue`/`block`/`ask` (or `reason`/
`stopReason`), `systemMessage`, `additionalContext`, and `suppressOutput`.
Anything policy-shaped (`permission`, `policy`, `approval`, `allowed_tools`,
`tools`, `env`, `config`, `sandbox`, `egress`, `model`, `provider`, …) is
**dropped and named** in `HookDecision.refused_policy_keys` and in the audit
row's `policy_mutation_refused` detail. `HookDecision` has no field that could
express a policy change, so this is structural, not a filter.

Degradation rules: an unknown decision word is an error inside
`HookDecision.from_payload`; unparsable output degrades to `continue` with a
`failed` row; exit code 2 with no parsable output is still honoured as a block
(a gate script's refusal is its point). **A hook can never block or allow by
accident.**

**Observational events cannot decide.** `PostToolUse`, `PostToolUseFailure`,
and `PreCompact` may add context and suppress output; a `block` on one is
rewritten to `continue` and the row records `outcome="ignored"` with
`suppressed_for_observational=True`, so the audit trail says a decision was
ignored rather than leaving the reader to infer it. Blocking events use
restrictive precedence with **strictly-greater** rank, so equal-rank
decisions are first-wins and the earliest blocker is recorded; every hook keeps
its own row, so the losing reason is still on the outcome.

**Latency is visible and budgeted.** Per-hook `duration_ms` and per-dispatch
`duration_ms` are on every record. `max_latency_s` (config, per tier) is spent
across a dispatch: once it is gone, remaining hooks are recorded as
`skipped` / `latency_budget_exhausted`, set `budget_exhausted=True`, and land
in `failures`. A hook that exceeds its own `timeout_s` is killed and recorded
(`exit_code=124`, "timed out after Ns").

**Failure is visible, bounded, and never silent.** A missing executable
(`127`), a spawn failure (`126`), a timeout (`124`), a raised handler, an
unusable payload, a refused egress, and a spent latency budget are all
`HookOutcome.failures` entries, all JSON-serializable for a trace row, and all
mirrored to the unified stream via `shared.tracing.emit("extensions",
"user_hook", ...)`. Ordinary failures are isolated: later hooks still run.

### Verifier integration

- `PostEditGate(engine).run(subject)` — runs the declared post-edit
  `PostToolUse` command hooks (the project's target test or lint subset) and
  returns a typed `PostEditGateReport` (`ran`/`passed`/`exit_code`/`reason`)
  instead of a string the caller has to re-parse. A non-matching subject
  reports `ran=False` with an explicit `skipped` reason.
- `CompletionGate(engine).evaluate(subject, status=..., verification=...)` —
  fires `Stop` and returns a `CompletionGateVerdict`. **A blocking completion
  hook prevents a false success**: `completed_verified` becomes
  `completed_unverified` when a `Stop` hook blocks. The gate **only ever
  downgrades**. No input to it can produce `completed_verified`, and a green
  `Stop` hook still cannot promote a run whose own verifier evidence is
  incomplete (`block_by="verifier_evidence"`). Defence in depth, because a hook
  layer is the least-trusted thing between a run and its verdict.

### `extensions/skill_policy.py` — versioned skill/agent declarations

- `SkillDeclaration` / `AgentSpec` carry `version`, `model_tier`, `tools`,
  `permissions`, `skills` and a declaration `digest`. A `SKILL.md` frontmatter
  block, a YAML flow list (`tools: [read, grep]`), a CSV list, a JSON list, and
  a YAML block sequence are all accepted. A malformed `version` is a recorded
  diagnostic, not a discovery failure — a skill file is untrusted content and
  must never be able to break discovery.
- `SessionPermissions` is the **parent session's envelope** in the same
  vocabulary the policy engine already uses. `resolve_permissions` intersects:
  a claim the session does not already permit is **refused and named** in
  `PermissionResolution.refused`, never granted, and there is **no
  configuration value that turns a refusal into a grant**. A `deny` claim
  always survives (it can only narrow), and the envelope's own `deny` entries
  are terminal for everything below them.
- `resolve_tools` intersects a declared tool list with the session's visible
  tool surface; an envelope that exposes no tools grants none.
- `clamp_tier` clamps a declared model tier **down** to the session ceiling.
  A skill can never promote itself to a more expensive model than the operator
  allowed for the session.
- `explain_selection(...)` is the record a `skills` trace row carries: which
  skills were considered, which matched, on which terms, at which
  version/tier, and for every refused claim the reason. An empty selection is a
  first-class record (`considered: 0` + an explicit `skipped` reason), the same
  receipt discipline the rest of the harness already follows.
- `load_agent_specs(root)` loads `agents/*.md` from an explicitly supplied
  root: a symlinked root, a symlinked entry, a non-`.md` file, an oversize
  file, and an unsafe name are all refused **and recorded** in `diagnostics`.
  It never raises.

### Wiring this module has (and does not have)

`harness/skills.py` now carries the four declaration fields on `Skill`
(`version`, `model_tier`, `permission_claims`, `declared_tools`), accepts a
YAML block sequence under `permissions:`/`tools:`, and reports a `declaration`
per matched skill in the existing receipt plus a `declarations` summary on
`build_skill_receipt`. **These fields are inert data on `Skill`.** Enforcement
lives only in `skill_policy`, which is what intersects them with the session
envelope — so a skill can *declare* anything and nothing here grants it. The
`Skill.__init__` signature is backward compatible (four new keyword-defaulted
parameters) and every existing caller is unchanged.

**Not wired, and that is a handoff, not a pass:** the strict kernel
(`harness/agent_kernel/kernel.py`) and the legacy loops do not yet call
`HookEngine`. The public API is `session_start`, `pre_tool_use`,
`post_tool_use`, `post_tool_use_failure`, `pre_compact`, `stop`,
`session_end`, and the exact integration point per event is in
`logs/ceiling/terminal-12.json`. Nothing in this module is imported by
`harness/` or `cli/`, which keeps the dependency direction clean (a regression
test asserts the source contains no `import cli` / `import harness`).

### Verification

`tests/test_ceiling12_hooks.py` → **35 passed, 1 platform skip** (the skip is
the Windows symlink-privilege case, not a pass). The six required behaviours
are `test_required_1` … `test_required_6`. No Docker, provider, or network lane
is required or claimed by this module.

## R2-16 — the declarative hook layer is WIRED, and its fail policy is stated (2026-09-26)

The previous section is explicit that nothing called `HookEngine`: the public
API existed, was tested directly, and was reached by no product code. That is
now closed for the call path this terminal owns, and the remaining half of the
problem — "what happens when a hook cannot run?" — has a documented answer per
event class instead of whatever the dispatcher happened to do.

### The fail policy (`HOOK_FAIL_POLICIES`, `HOOK_FAIL_POLICY_REASONS`)

A hook that raises, times out, is missing its executable, returns unusable
output, or finds the latency budget spent has **not approved anything**. For
an event whose whole purpose is to gate, treating that silence as consent is
the one failure mode a hook layer must not have.

| event | policy | why (from `HOOK_FAIL_POLICY_REASONS`) |
|---|---|---|
| `PreToolUse` | **fail-closed** | this event IS the gate on a tool call |
| `Stop` | **fail-closed** | this event gates completion |
| `PostToolUse` | fail-open | observational: can add context / suppress output, never decide |
| `PostToolUseFailure` | fail-open | observational: the tool already failed; it reports |
| `PreCompact` | fail-open | observational: compaction is harness bookkeeping |
| `SessionStart` | fail-open | lifecycle: nothing is gated |
| `SessionEnd` | fail-open | lifecycle: teardown reports |

Overrides, highest precedence first, and the winning source is on every gate:

1. the registration's own `fail_policy` → source `hook:<id>`
2. a tier's `"fail_policy": {"<Event>": …}` table → source `config:event`
3. a tier's `default_fail_policy` → source `config:default`
4. `HOOK_FAIL_POLICIES` → source `table`

`HookEngine.gate(event, subject) -> HookGate` is the only object a caller
should branch on: `allowed` already has the policy folded in, so a caller
cannot accidentally treat an absent verdict as consent. **An observational
event can never refuse even if its policy is `fail_closed`** — the structural
"observational cannot decide" rule is deliberately not made conditional, and
the failure is still reported on `reason` and `failures`.

`CompletionGate.evaluate` now routes through `gate()`. A broken `Stop` hook
therefore downgrades `completed_verified` → `completed_unverified` with
`blocked_by="fail_policy"`. It still only ever downgrades: a status that was
already unverified/failed/timeout/cancelled comes back unchanged, and no input
produces `completed_verified`. `CompletionGateVerdict` gained additive
`fail_policy` / `fail_policy_source`.

### The wiring, and exactly where

`cli/connectors.py::call_tool` is the real tool-call path this terminal owns
(`neo mcp call`). It fires, in this order:

1. `SessionStart` — once per connector label per process
2. `PreToolUse` — **before any server process is spawned**; a blocking or
   fail-closed verdict refuses the call and the server never launches
3. `PostToolUse` / `PostToolUseFailure` — after the dispatch resolves

Every dispatch's receipt rides on `ConnectorReceipt.hooks`, so a trace can be
read without re-running anything. If the `extensions` package is not importable
(an install that does not ship it) or a hook config is unparseable, the
connector surface degrades to "no hook layer" and says so in the receipt as
`hooks_unavailable` — an unusable hook LAYER must never become a crash on the
call path.

The strict kernel (`harness/agent_kernel/kernel.py`) and the legacy loops
still do not call `HookEngine`. That is filed as a cross-terminal request in
`cli/AGENTS.md`, with the exact integration points; the public API is
`session_start`, `pre_tool_use`, `post_tool_use`, `post_tool_use_failure`,
`pre_compact`, `stop`, `session_end`, plus the new `gate`.

### `neo hooks list|run`

`hooks list` prints the merged config, every registration with its tier /
timeout / effective fail policy and its source, and the documented table with
each reason — so the operator can see WHICH policy is in force before a hook
surprises them. `hooks run <Event>` fires one event through the same gate the
connector surface uses and exits 1 when the gate refuses.

### Verification

`tests/test_r2_16_extension_ops.py` → **34 passed**; of those, the hook
behaviours are `test_a_hook_that_raises_is_handled_per_its_documented_class`
(a `prompt` handler whose injected renderer raises, registered against BOTH a
fail-closed and a fail-open event), `test_every_hook_event_has_a_documented_
fail_policy_and_a_reason`, `test_a_missing_executable_is_a_failed_hook_not_a_
passed_one` (a real subprocess 127), `test_an_operator_can_override_the_fail_
policy_and_the_override_is_recorded`,
`test_a_per_hook_fail_policy_beats_the_event_policy`, and
`test_a_broken_stop_hook_cannot_leave_a_verified_status_intact`.
`tests/test_ceiling12_hooks.py` → 36 passed, 1 platform skip, unmodified.
`tests/test_extensions.py` → part of 67 passed with `test_cli_plugins.py`, 2
platform skips. No Docker, provider, or network lane is required or claimed.

### Not implemented / honest

- **The kernel is still unwired.** See the cross-terminal request. **UPDATED:
  see "Handoff to integration" in the VEX-CS-09 section below**, which publishes
  the exact call sites, signatures and the gate object.
- **No `mcp__<server>__<tool>` matcher convenience.** A hook matches an MCP
  call through `mcp_server`, which is the connector LABEL, not the namespaced
  tool id. Matching the namespaced id means writing two matchers.
- **A `prompt` handler still needs an injected renderer** to do anything; with
  none it is skipped and the skip is recorded (pre-existing, unchanged).
- `HOOK_FAIL_POLICIES` is a module-level table, not a config value. It is a
  security policy: a configuration value that flipped it would let a repository
  opt itself out of the gate it is subject to. **VEX-CS-09 kept that property and
  made the refusal structural: the table is derived from a per-event CLASS in
  `extensions/hook_events.py`, and a PLUGIN that declares a policy is recorded
  and refused.**


- The current host can create and run the extension tests without Docker or provider credentials. The symlink test may skip when the Windows privilege is unavailable.
- Packaging metadata was intentionally not changed by that round because it was restricted to `extensions/`, `tests/test_extensions.py`, and the handoff file. **Checked and CORRECTED in R2-16: `extensions` IS in `pyproject.toml`'s `[tool.setuptools] packages` (line 109), so the package DOES ship in the wheel and the "an installation owner must add `extensions` to packaging discovery" note above is STALE — it is no longer a handoff.** The `cli/connectors.py` fallback to "no hook layer" (reported as `hooks_unavailable` in the receipt) is therefore a defensive path for a broken install or an unimportable package, not a packaging gap, and the reasoning for keeping it is unchanged.
- The kernel, CLI, harness, runtime, and shared contracts were not edited. A runtime owner integrating the lifecycle should call `HookManager.before_task`, `before_tool`, `before_permission`, and completion dispatch at the authoritative boundaries, then pass the original result through through after/completion hooks.
- `INTERFACES.md` was not edited under the path restriction. If the new extension surface becomes a cross-module contract, the contract owner should document the hook context/result and plugin lifecycle handoff there. **VEX-CS-09 also did not edit it; its `AGENTS.md` line above stands.**
- Callers that need local files must provide `root` and an import allowlist explicitly. Callers that only need registration should use `PluginSpec` or an injected factory and avoid filesystem loading.
- No marketplace behavior is a deliberate scope decision, not an omitted implementation.

---

## VEX-CS-09 — `/hooks` as verbs, and the kernel gate published (2026-10-01)

**Files this round owned and created:** NEW `extensions/hook_events.py`;
`extensions/user_hooks.py` (additive regions only — the three symbols below are
the only existing ones whose bodies changed, and each is listed with its reason);
`cli/main.py` (**ONLY** the `cmd_hooks` body and the `neo hooks` parser block);
NEW `tests/test_hooks_command.py`; `logs/command-surface/terminal09_measure.py` +
`terminal-09-measure.json`. **`harness/agent_loop.py`,
`harness/agent_kernel/kernel.py`, `cli/commands.py` and `cli/tui.py` were NOT
opened for edit.** No `INTERFACES.md` Change Log entry, no
`harness/config.py` `DEFAULTS` key, no event kind, no journal field, no
completion status, no verifier mint, no exit code added.

Machine-readable handoff: **`logs/command-surface/terminal-09.json`**.
Measurements: **`logs/command-surface/terminal-09-measure.json`**, produced by
`terminal09_measure.py` (host-only: no Docker, no provider, no network, no
credential; the only subprocesses are this interpreter running hook commands).

### 0. The six numbers, before anything else

| measurement (this host, 3.10.11, win32, 12 cores) | number |
|---|---|
| `gate()` with **no** matching hook (2000 samples) | **0.036 ms median / 0.045 ms p95** |
| `gate()` with one registration that does **not** match | **0.034 ms median / 0.062 ms p95** |
| `gate()` with one **matching** hook (30 samples) | **161 ms median / 215 ms p95** — a real subprocess |
| `gate()` with a **user + plugin** stack (20 samples) | **240 ms median / 311 ms p95** — two subprocesses |
| a hook that **hangs** (child sleeps 30 s, `timeout_s` 0.5) | dispatch returns in **522 ms median** |
| the eleven-event vocabulary, rendered whole | **0.027 ms median / 0.030 ms p95** |

The number that matters for whoever wires the kernel is the FIRST one: the
gate's own marginal cost on a tool call is **~0.04 ms**, and a declared hook's
cost is a subprocess at **~160 ms** on this host. Both are stated as measured
medians with their sample counts, and the second is not a claim that hooks are
free — it is the cost a `PreToolUse` gate actually pays, and it is why
`max_latency_s` and per-hook `timeout_s` exist. On a four-terminal Windows host
the spread on the subprocess arms is wide (p95 ≈ 1.3x median); treat those three
as orders of magnitude, not as budgets.

### 1. The vocabulary, and why it lives in its own module

NEW `extensions/hook_events.py` is DATA with no dependency on any other module
(this tree's dependency direction forbids it reaching into `cli`/`harness`, and
a test pins that with a source scan). It owns:

* `LIFECYCLE_EVENTS` — the closed eleven-event vocabulary, in lifecycle order.
  The four new names are `UserPromptSubmit`, `Notification`, `SubagentStart`,
  `SubagentStop`.
* `EVENT_CLASSES` — every event gets a CLASS: `gating | observational |
  lifecycle`. The fail policy is **derived from the class**, never restated per
  event, so a twelfth event cannot inherit `fail_open` by accident: it has to be
  given a class, and `fail_policy_for` RAISES for an event with no declared
  class. That raise is the whole requirement, stated as a gate.
* `FAIL_POLICY_VALUES_BY_CLASS` — `gating → fail_closed`, the other two →
  `fail_open`.
* `FAIL_POLICY_REASONS` / `EVENT_DESCRIPTIONS` — one written reason and one
  plain description per event, so `/hooks list` can teach the vocabulary from a
  table rather than from source.
* `FAIL_CLOSED_EVENTS = ("PreToolUse", "Stop", "UserPromptSubmit")` — named so a
  test pins the security property directly rather than re-deriving it.
* `normalize_event` — every spelling a person or a manifest may use
  (`pre_tool_use`, `before_tool`, `tool_before`, …). An unknown event is a
  `HookEventConfigError` naming the vocabulary, never "no hooks configured".
* `synthetic_subject(event, **overrides)` — the per-event subject `/hooks test`
  runs against. Reproducible on every host, field-overridable, and an unknown
  override key is REFUSED rather than silently dropped.
* `plugin_hook_document(name, manifest)` — a plugin's declared hooks, with every
  id namespaced to `plugin:<plugin>:<id>`. A malformed declaration is a
  **diagnostic**, never an exception: an installed plugin must not be able to
  break the session that merely loads it.

`user_hooks.HookEvent` is now **derived** from `LIFECYCLE_EVENTS` through the
functional Enum API (a class body cannot synthesise members from an imported
tuple, and a restated list is a second place to forget an event), with
`observational`, `event_class`, `__str__` and `_missing_` attached from the same
authority. `HOOK_LIFECYCLE_EVENTS` is the full eleven; `HOOK_EVENTS` is the
legacy seven, **derived** from `hook_events.LEGACY_EVENTS` and unchanged, because
shipped code and two existing suites read that name and expect those seven.
`HOOK_FAIL_POLICIES` / `HOOK_FAIL_POLICY_REASONS` are projections of the
authority over those seven. `HookConfig.to_dict()` keeps its historical keys
byte-for-byte and ADDS `lifecycle_events`, `event_classes`,
`lifecycle_fail_policies` and `lifecycle_fail_policy_reasons`.

### 2. `/hooks` — five verbs, ONE implementation

`hooks_command(argv, ...) -> HookCommandResult` is the primary implementation.
`cli/main.py::cmd_hooks` **delegates to it** and does nothing but map argparse
attributes onto the verb's flags and print. The slash verb that Terminal 01
mounts calls the same function, so there is no second behaviour to drift.

| verb | what it does | exit |
|---|---|---|
| `list` | merged config, tier precedence, every registration with its tier / timeout / effective fail policy AND ITS SOURCE, the whole eleven-event vocabulary with each class and reason, every hook's trust state, diagnostics, config sources | 0 |
| `run <Event>` | fires the real `HookEngine.gate`; prints the verdict, the rewritten call if there is one, and one row per record | 0 allow / 1 refuse / 2 usage |
| `test --id <h>` | runs **ONE** named hook against a synthetic event and shows its real argv, exit code, duration, decision, context, and one plain sentence | 0 ran / 1 failed / 2 usage |
| `trust [--id <h> --decision trusted\|untrusted]` | no `--id` reports every hook's decision and digest; with `--id` records it | 0 / 2 |
| `reload [--repo R]` | re-reads every tier and re-registers, reporting added / removed / changed by DIGEST | 0 / 2 |

`test` is the verb the brief is really about: **trusting a hook you have never
run is the worst way to use one**, and "it works" is a different question from
"it does what I meant". It runs exactly one registration — not the whole event —
and reports `would_match` separately, so "would my matcher have fired this" and
"does my hook do the right thing" are two visible answers rather than one
conflated one. A hook whose matcher would NOT have selected the synthetic
subject is still run, because that is what "try it before trusting it" means.

`reload` exists because the alternative to restarting a long session to pick up
an edited hook is not editing hooks. A config that has become **unparseable** is
REFUSED and the previous registrations stay in force — a reload that quietly kept
the old config would be a lie about what is loaded. `HookRegistry` is the live
holder for a session that wants it; `adopt` swaps a whole config reference, which
is atomic under the GIL and cannot be observed half-applied.

### 3. Trust: bookkeeping about a DECLARATION

`record_hook_trust(id, decision, digest, note=...)` writes
`<user hooks dir>/hook-trust.json` (mode 0600 best effort), under an `O_EXCL`
sibling lock, staged to a unique temp name and `os.replace`d. The digest covers
the **handler** as well as the registration metadata, because
`HookSpec.to_dict()` deliberately omits the argv — a digest built from the
receipt alone could not tell a hook whose command had been edited from one that
had not, which would carry a trust decision over to code the user never read.
That was a real bug the round's own round-trip test found.

A trust decision may be `trusted` or `untrusted` and nothing else: an unusable
word is REFUSED, not coerced, because a silently coerced value would be an
authorisation nobody gave. There is no "trusted by plugin" value — a plugin
cannot vouch for itself. **A hook can DENY and ask; nothing here can ALLOW
anything.** Recording trust authorises a hook the operator could simply have
declared, so it is a consent and bookkeeping surface and never a privilege
grant.

### 4. The hooks STACK: a plugin refines the user's gate

`load_hook_config(..., plugin_manifests={name: manifest})` merges plugin hooks as
a fourth tier, `plugin`, **appended** to the precedence tuple so the three
shipped tiers keep the exact relative order and ranks they have always had. A
plugin's `PreToolUse` hook and the user's own **both fire**, ordered user →
project → local → plugin, and every surviving registration keeps its tier so a
receipt names whose hook ran.

The load-bearing detail is the **id namespacing**: the merge replaces a
registration whose id collides, so a plugin that reused a user's id would
silently disable that user's gate. Namespacing makes the collision unreachable
rather than merely unlikely. A plugin also **cannot declare a fail policy** — the
keys are dropped while the manifest is read and the attempt is recorded as
`plugin_fail_policy_refused`, because a value a plugin could write would let that
plugin opt itself out of the gate it is subject to.

### 5. Rewrite: `PreToolUse` can BLOCK or REWRITE

A hook can block a call, or make it narrower than the model asked for — "never
run `git push` from this repo" and "never write above `src/`" are not expressible
as a refusal. `HookRewrite` has **exactly two fields** and both can only
narrow:

* `command_prefix` — accepted only when
  `shared.approval.command_prefix_matches` says the new prefix is a real prefix
  of the call's own. That is the SAME matcher the policy engine and the CLI use,
  so "what a rewrite may narrow to" and "what a grant may cover" cannot disagree,
  and this round did not weaken the declared boundary check.
* `path` — accepted only when the new path is the original or lies inside its
  tree.

A candidate that would **widen** either is refused BY NAME in
`refused_rewrite_keys` and on the record's detail; a key outside the two is
refused too. There is no field that could add a capability, so this is
structural rather than a filter. A rewrite is dropped on an **observational**
event, exactly as a `block` is, because a rewrite changes the call.

`HookGate` gains `rewrite` (who narrowed what) and `rewritten_subject` (the call
that will ACTUALLY run) plus `subject_overrides()`. **A caller must dispatch
`rewritten_subject`, not the subject it passed in** — and `/hooks run` prints it,
so the receipt shows what ran rather than what was asked for. `HookGate.refusal()`
is the one model-facing sentence a denied call produces, so a terminal, a script
and a model cannot receive three different accounts of one refusal.

### 6. Plain-language failure

Every failure path reduces to one sentence through `plain_hook_sentence`, built
from a **closed vocabulary** (`_FAILURE_PLAIN`) rather than from the failure's
text. A detail may only qualify the sentence if it is short, single-line, and
does not match `_PLAIN_TRACEBACK`; otherwise it is dropped from the sentence and
kept on `HookRecord.detail`, where it is diagnostic and never run state. A test
writes a real `ZeroDivisionError` traceback to a real child's stderr and asserts
the sentence contains none of it while the detail still contains all of it.

`HookRecord` gains `plain` and `rewrite`; both are in `to_dict()`. A refusal is
**always emitted**: `_usage_result` / `_refused_result` write their own lines, so
a broken hook config cannot become a command that prints nothing and exits 2 —
which is indistinguishable from a crash to whoever is watching. That was found by
running `/hooks` against a config with a `/` in a hook id.

### 7. Three real defects the round's own surface found, and their fixes

1. **A reason-only payload was rejected as unusable.** `_DECISION_KEYS` included
   `reason`/`stopReason` (for the bare-string output form), so in a JSON object
   the most natural gate output in the world — `{"reason": "never push from this
   repo"}` — raised `unsupported hook decision` and was discarded. Found by
   writing `/hooks test` against a real gate, not by reading the code. A mapping's
   decision is now read from `("decision", "continue", "block", "ask")` only, and a
   reason alone always yields `continue` — which is also the safe direction, since
   a hook must not block by accident.
2. **`HookSubject` rendered every absent field as the string `"None"`.**
   `_text(None)` is `str(None)`, so a subject that declared no status carried the
   literal word `None` into every receipt, and a matcher written against it would
   have matched a call that declared nothing. `_subject_field` maps absent to
   `""`. Measured on a real `/hooks test` receipt.
3. **The trust digest could not see the argv.** Covered in §3.

A fourth was mine and is worth recording: an early `Set-Content -Encoding utf8`
append on this PowerShell 5.1 host **wrote a UTF-8 BOM** into
`extensions/user_hooks.py`, which `ast.parse` then refused. `git show HEAD`
confirms HEAD had no BOM, so it was introduced here and stripped; all four
touched files are now asserted BOM-free with LF endings by
`tests/test_hooks_command.py`'s sibling check and by the measurement driver.

### 8. Backward compatibility — what was preserved, and how it was checked

* All 52 commands, the 8 aliases, the 13 `HEADLESS_FLAG_EQUIVALENTS` rows and all
  six exit codes are untouched: this round did not edit `cli/commands.py`, and
  the only exit codes it can return are 0, 1 and 2.
* `HOOK_EVENTS` is still the seven shipped names, and
  `test_ceiling12_hooks.py::test_every_declared_event_is_dispatchable` — which
  pins that set exactly — passes unmodified.
* `HOOK_FAIL_POLICIES` still has exactly the seven keys, and
  `test_r2_16_extension_ops.py::test_every_hook_event_has_a_documented_fail_policy_and_a_reason`
  (which pins key-set equality with `HOOK_EVENTS`) passes unmodified.
* **The `--json` document shape is preserved.** This was a real break, found by
  running the R2-16 suite: my first `cmd_hooks` printed the verb envelope, so
  `payload["allowed"]` no longer existed at the top level. `HookCommandResult`
  now carries a `document` — the merged config for `list`, the gate receipt for
  `run` — spread into the TOP LEVEL of `to_dict()`, with the envelope additive.
  `tests/test_hooks_command.py` pins the historical top-level keys **and** the
  new ones, so a future change cannot quietly move either.
* `PostEditGate` and `CompletionGate` are behaviourally unchanged.
  `CompletionGate` still only ever DOWNGRADES, and an AST pin asserts the only
  completion vocabulary in executable text in this module is the pre-existing
  `CompletionGate.VERIFIED` / `UNVERIFIED` pair.

### 9. Handoff to integration (the kernel gate) — EXACT call sites

`harness/agent_kernel/kernel.py` and `harness/agent_loop.py` are another owner's
files. **They were not opened.** Everything below is built, tested and inert
until they are wired. The public API is one call:

```python
from extensions.user_hooks import HookEngine, HookRegistry, HookSubject, HookEvent
gate = HookEngine(config, repo_path=repo).gate("PreToolUse", subject)  # -> HookGate
if not gate.allowed:
    ... refuse with gate.refusal() ...
dispatch(gate.subject_overrides() or the_original_call)   # <-- dispatch the NARROWED call
```

| # | file | where | call |
|---|---|---|---|
| 1 | `harness/agent_kernel/kernel.py` | immediately BEFORE a tool handler is invoked, once per tool call, with the call projected into `HookSubject(tool=, path=, command=, command_prefix=, mcp_server=, network_domain=, side_effect_class=, task_id=, session_id=)` | `gate = engine.gate("PreToolUse", subject)`; **do not invoke the handler unless `gate.allowed`**; pass `gate.subject_overrides()` into the call |
| 2 | same | immediately AFTER the handler resolves, on success | `engine.gate("PostToolUse", subject_with_result)` |
| 3 | same | immediately AFTER the handler raises or returns a failure | `engine.gate("PostToolUseFailure", subject_with_error)` |
| 4 | same | before context compaction runs | `engine.gate("PreCompact", subject)`; `PreCompact` is observational, so it can only add context and suppress output — it can never refuse |
| 5 | same | before a terminal status is MINTED, not after | `verdict = CompletionGate(engine).evaluate(subject, status=<the status about to be minted>, verification=<the run's own evidence dict or None>)`; **report `verdict.status`, not the status you passed in** |
| 6 | same | when a subagent is created / returns | `engine.gate("SubagentStart", ...)` / `engine.gate("SubagentStop", ...)` — both lifecycle, both fail-open, both purely observational |
| 7 | same | when the shell is about to notify the user | `engine.gate("Notification", subject)` — observational; it can add `system_message` context to the notification and nothing else |
| 8 | the interactive path, when a prompt is submitted | before the run starts | `engine.gate("UserPromptSubmit", subject)`; **fail-closed**, like `PreToolUse` |
| 9 | any long-lived session that wants `/hooks reload` to matter | session setup | `registry = HookRegistry(repo_path=repo, plugin_manifests=load_enabled_plugin_manifests())` and then call `registry.gate(event, subject)` — `HookRegistry.gate` delegates to the currently-registered engine, so a reload reaches a running session |
| 10 | `cli/connectors.py::call_tool` (already wired by R2-16) | — | **unchanged, and it keeps working**: it still builds its own `HookEngine` per call and still reports `hooks_unavailable`. To make the connector share one registry with the kernel, pass the registry in; nothing else changes |

Three rules for whoever wires these:

* **`gate` is the only thing to branch on.** `allowed` already has the event's
  fail policy folded in, so treating an absent verdict as consent is not
  expressible.
* **`Stop` must be evaluated BEFORE the status is minted**, and the status
  reported must be `verdict.status`. `CompletionGate` only ever downgrades, and
  `completed_verified` is unreachable from it — so wiring it late cannot promote
  anything, and wiring it at all is what stops a broken `Stop` hook leaving a
  verified status intact.
* **Route every external MCP call through the same gate.** `cli/connectors.py`
  already is gated; the kernel's own tool dispatch is not, which is the whole gap
  this round closes.

### 10. Handoff to 01 — the `/hooks` SLASH verb (not mounted here)

`cli/commands.py`, `cli/interactive.py` and `cli/tui.py` are Prompt 01's and were
not opened. `cli/main.py` cannot add a slash command. Everything needed is here.

**One row.** `CommandSpec("/hooks", ...)` with
`aliases=("/hook",)`, `headless="flag-only"`, `interactive_dispatch="handled"`,
`in_flight_policy="allow"` (a person edits a hook *because* a run is misbehaving),
`argument_policy="required"`, `result_presentation="inline"`, and
`argument_hint="/hooks list|run <Event>|test --id <hook>|trust|reload"`. The row
belongs in `SUBCOMMANDS` as five `SubcommandSpec`s so `/hook te` completes.

**One handler**, delegating to this module — no logic of its own:

```python
def hooks_command_line(line: str, repo: str, log_root: str) -> int:
    from extensions import user_hooks as hooks
    tokens = line.split()[1:]                      # drop "/hooks"
    result = hooks.hooks_command(tokens, repo_path=repo)
    for text in result.lines:                      # already markup-escaped
        con.print(text)
    return result.exit_code
```

* The verb is **already discoverable**: `hooks_command` is what prints the verb
  list, `/hooks frobnicate` names all five, and `hooks_list` prints the tier
  precedence, so a user who types `/hooks` with no argument gets the whole
  vocabulary rather than a usage error (`hooks_command` defaults to `list`).
* **Escape, or use the `lines` as-is.** Every line is already passed through
  `rich.markup.escape`; printing them unmodified is safe and is what
  `cmd_hooks` does. Do NOT re-interpret them.
* The TUI wants a transcript, so pass
  `write=lambda line: self.transcript(Text(line))` — `rich.text.Text` has no
  markup interpretation at all, which is the structural answer for a TUI.
* A `HookRegistry` per session (§9 row 9) makes `/hooks reload` reach the
  running session instead of only reporting a delta.

**Two mount points that are NOT optional together.** `test_cli_terminal_parity.py`
parses both dispatchers with `ast` and requires the two branch-key SETS to be
equal, so a REPL-only branch is a command one shell has and the other does not.
Either mount both shells or neither.

### 11. Handoff to the plugin owner — the `hooks` manifest key is not read yet

`hook_events.plugin_hook_document` turns a manifest's `hooks` key into tier
documents, and `load_hook_config(plugin_manifests=...)` merges them. **Nothing in
the product yet builds that mapping**: `cli/plugins.py` /
`cli/plugin_runtime.py` do not read a `hooks` key, and they are not this round's
files. The mount is one call wherever a plugin manifest is already read:

```python
from extensions import user_hooks as hooks
config = hooks.load_hook_config(
    repo_path=repo,
    plugin_manifests={name: manifest for name, manifest in enabled_manifests},
)
```

`enabled_manifests` must be the **enabled** plugins only — a disabled plugin's
hook must not fire, and a disabled marker is checked directly in
`cli/plugins.py`'s other discovery passes.

### 12. Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)

- **Required lane 1** `tests/test_ceiling12_hooks.py tests/test_extensions.py` →
  **54 passed, 2 skipped** (the 2 are the pre-existing Windows symlink-privilege
  platform cases and are NOT counted as passes).
- **Required lane 2** `tests/test_hooks_command.py` → **95 passed** (host-only: no
  Docker, no provider, no network, no credential). One class per required proof:
  `TestEveryDeclaredEventFiresOnARealPath` (10),
  `TestTheFailPolicyIsDeclaredPerClassAndPinned` (8),
  `TestTheHooksStack` (7), `TestPreToolUseCanBlockAndCanRewrite` (10),
  `TestAHookCannotGrantPermission` (5),
  `TestAHookTimeoutResolvesByItsPolicy` (4),
  `TestFailureIsOnePlainSentence` (9),
  `TestHooksTestRunsOneHookAndShowsRealOutput` (7),
  `TestHooksTrustRecordsTheDecision` (6),
  `TestHooksReloadReRegistersWithoutARestart` (4),
  `TestTheVerbSurfaceIsOneImplementation` (7),
  `TestMarkupSafetyThroughARealConsole` (5), `TestStructuralInvariants` (8),
  `TestTheScriptSurfaceDelegatesToTheVerbs` (7).
- `tests/test_r2_16_extension_ops.py` → **34 passed**. This lane is what caught the
  `--json` shape break in §8.
- `tests/test_cli.py tests/test_cli_errors.py tests/test_cli_release.py` → **82
  passed**.
- `tests/test_cli_command_system.py tests/test_cli_slash2.py
  tests/test_cli_terminal_parity.py` → **181 passed**. The three lanes that pin
  the 56-command registry, the 8 aliases and the flag-equivalent tables are green
  against the `cli/main.py` parser change.
- `tests/test_cli_power_tools.py tests/test_cli_neo3.py` → **110 passed**.
- `python -m evals.run --check` → **14/14 CLEAN**, exit 0. No prompt changed, so
  this is a no-regression receipt and not a claim about model quality.
- `python -m ruff check` on all four owned/created Python files → **All checks
  passed**. `ruff format` applied to all four (including
  `extensions/user_hooks.py`, which is this round's declared file, so a
  whole-file format cannot disturb another terminal's region).
  `python -m compileall -q` clean on all four. `git diff --check -- cli/main.py`
  → **exit 0** (the only output is the shared tree's LF/CRLF warning).
  The four untracked files are asserted BOM-free, LF, and free of trailing
  whitespace.
- `python logs/command-surface/terminal09_measure.py` → wrote
  `logs/command-surface/terminal-09-measure.json`, read back and summarised in §0.

#### 12.1 Two reds, attributed, and NOT counted as passes

Both are in files this round did not edit, and both are in another terminal's
documented in-flight work. Neither traceback enters `extensions/`.

1. `tests/test_cli_connectors.py::test_duplicate_plugin_labels_keep_winning_source`
   — `cli/plugin_runtime.py::validate_manifest_identity` (an **untracked** file,
   `??` in `git status`, not mine) now requires a manifest to declare BOTH
   `name` and `version`, and the test's fixture declares only `name`. The
   failure is a `PluginError` raised before any hook code is reached.
2. `tests/test_cli_plugins.py::test_slash_unknown_lists_available_customs` — the
   unknown-command preflight prints the error and the recovery hint but not the
   `custom commands available` list. This is the exact defect
   `cli/AGENTS.md` records under **VEX-CS-01 §10.1** as another terminal's R2-18
   in-flight work, and the captured output is verbatim the shape that note
   describes. The failing file contains no reference to anything this round
   added.

**Not run, and not claimed:** no Docker lane; no live-provider lane with a real
credential (no credential was inspected, printed or retained); no full
`python -m evals.run` matrix (no prompt changed, so it is unchanged by
construction as well as by the `--check` receipt); **no full-suite run**; no
real attached-PTY / ConPTY campaign. Every figure in §0 is from the host-only
driver on the final tree.

#### 12.2 What is deliberately NOT done

- **The kernel is unwired.** §9 publishes the nine call sites; wiring them is
  another owner's file and this round did not open it. Until then the daily
  path's own tools are still ungated, which is the gap R2-16 §6.3 filed.
- **`/hooks` is not a slash command yet.** §10 is the exact mount. `neo hooks`
  works today for all five verbs.
- **The plugin `hooks` manifest key is not read by any product code.** §11.
- **No `mcp__<server>__<tool>` matcher convenience**, and no `Notification`
  wiring — the event exists, is classed, is documented and is dispatchable, and
  nothing calls it yet (§9 row 7 is the mount).
- **A rewrite cannot ADD capability**, only narrow. A hook that wants a wider
  call, a different tool, or a permission has no expression and gets a named
  refusal. Widening is the user's decision, made in the config.
- **Trust does not disable anything.** Recording `untrusted` records the
  decision; the hook still runs if it is declared, and the surface says so in
  those words. Disabling a hook is what removing it from the config is for.
- **No `DEFAULTS` key.** The fail-policy table is a security property of an
  EVENT and stays a module-level table: a value in `DEFAULTS` merges into every
  task and every eval arm, and a configuration value that could flip it would
  let a repository opt itself out of the gate it is subject to. The knobs this
  round added are all CLI flags, per-hook config, or module constants.
