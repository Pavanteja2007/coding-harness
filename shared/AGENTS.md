# AGENTS.md — shared/: versioned agent contracts and structured tracing

## The VEX -> NEO rename: `shared/brand.py` (2026-10-02)

**New file: `shared/brand.py`.** No other module's signature changed. The
canonical names moved (command, distribution, `NEO_*` prefix, `.neo/`,
`~/.neo`) and the previous spellings are still READ.

`shared/` is where this file lives because it is the bottom layer: `cli`,
`harness`, `runtime`, `memory`, `execution` and `mcp_server` all may import
it and it imports none of them. It is also the only place that can be
imported by `cli`, `mcp_server`, `dashboard` and `evals` alike without any
of them depending on each other.

### Why one function instead of thirty call sites

There are ~30 places that read configuration from the environment, and they
each read a `NEO_*` name. Threading a legacy fallback through all of them
means a compat table that has to be edited every time a knob is added, and a
knob that is added without editing it is a knob whose legacy spelling is
silently ignored. `apply_legacy_env()` is instead called ONCE per process
from the four entry points (`cli.main.main`, `mcp_server.server.main`,
`dashboard.server.main`, `evals.run.main`), before any parser or config
read, and it is generic over the prefix: a `NEO_*` variable added in 2030
is honoured from `VEX_*` with no code change.

**It mutates `os.environ`.** That is deliberate and it is the reason the
call site is the entry point rather than a decorator: a shim applied at read
time has to touch every reader, and a shim applied at import time runs in
the wrong order for anything that reads configuration during import.
`tests/test_brand_rename.py` passes an explicit mapping, so the test never
depends on process state.

### The four rules, and why each one is load-bearing

1. **New is written, old is read.** Nothing in the tree writes `VEX_*`,
   `.vex/` or `vex-harness` any more. A rename that also keeps WRITING the
   old name never finishes.
2. **The new name always wins** (`apply_legacy_env`, and every `_env_path`
   / `os.environ` fallback added for it). If both are set, the new one is
   used and the legacy one ignored. Honouring the old spelling when the user
   has set the new one is how a "which one do I edit" bug becomes permanent
   - the user has no way to tell which one the product used. `legacy_names_report`
   separates `legacy_env_honoured` from `legacy_env_shadowed` so a receipt
   can never claim a legacy variable is in effect when it is being ignored.
3. **The fallback is a READ path.** It never migrates, copies, renames or
   writes into a legacy location. `neo_home()` returning a legacy directory
   does not create the new one. Silently moving a user's state is how a tool
   loses it, and a user who wants the new layout points `NEO_HOME` at it.
4. **A non-mapping is REFUSED, not ignored.** `apply_legacy_env(["a"])`
   raises `TypeError`. A compat shim that quietly does nothing on a
   programming error is a shim that claims to have run and did not.

`legacy_names_report()` is read-only and total, and is what a surface or a
test should read instead of re-deriving the prefix rules in
`os.environ`. `deprecation_notice()` emits once and then empties itself,
because a deprecation notice that fires per call site is a warning storm, and
one that does not name the replacement is a notice nobody can act on.

### What is NOT in this module, and why

The on-disk fallbacks are NOT here. `shared/brand.py` exports
`legacy_home_candidates()` and `legacy_project_dirname()` as NAMES, not
paths, because `memory.paths` is the single location authority and a second
one is how two paths end up meaning the same directory and disagreeing
about it. The actual resolvers live with the code that owns them:
`memory.paths.neo_home()`, `cli.neoconfig.project_settings_dir()`,
`global_settings_path()` and `legacy_settings_path()`.

**One behavioural change worth naming, because it fixed a pre-existing red.**
`cli.neoconfig.project_settings_dir()` previously walked the ancestor chain
UNBOUNDED. That is reachable in practice: a very common setup is a dotfiles
git repository at `$HOME`, and then a session in any temp directory resolved
its project tier to `$HOME/.neo` - the same settings file for every
unrelated project. The walk is now bounded to the enclosing git repository
(inclusive), and both the `.neo` and the `.vex` branch obey the same bound,
so the current and previous names can never resolve to different scopes. An
explicit `$NEO_PROJECT_DIR` is still honoured ABOVE the bound, which is how a
non-repository location is opted into.

### Verification

`tests/test_brand_rename.py` -> **27 passed**, host-only: no Docker, no
provider, no network, no credential. It covers the canonical names, the
declared legacy names, the packaging metadata, argparse's program name (a
real child process), every `apply_legacy_env` rule including the
empty-value and precedence cases, the generic prefix property, the report's
honoured/shadowed split, all four on-disk fallbacks driven through the real
resolvers, and both wordmark variants.

Not claimed: no Docker lane, no live-provider lane, no full-suite run, and
no real-PSY campaign. The one assertion that drives a real child process
(`test_the_cli_reports_neo_as_its_program_name`) is the only subprocess
evidence here.

---

## VEX-PF-08 - platform: one writer per work tree, and an honest unavailability (2026-09-29)

**Files this round created in `shared/`:** NEW `shared/instance_guard.py`, NEW
`shared/availability.py`. **Nothing else in `shared/` was edited.** VEX-PF-07
added `shared/platform.py` in the same window; it is untouched here. No
`INTERFACES.md` Boundary 0-5 signature, event kind, journal field, serialized
field, completion status, exit code or verifier mint changed, and **no
`harness/config.py` `DEFAULTS` key was added** (see section 5).

Machine-readable handoff: `logs/product-round/terminal-08.json`.

### 1. `shared/instance_guard.py` - the multi-instance guard, and WHY it is here

Two `neo` processes writing one repository is silent data loss: two sessions
each believing they own `logs/<task>/work`, a checkpoint taken over a file the
other session is mid-write, and a diff that mixes both. This module makes that
state **detectable and refuses it**, so the second instance stops before it has
written anything. It lives in `shared/` because `cli`, `execution`, `harness`
and `runtime` may all need it and it imports none of them - the same
bottom-layer position as `shared/security.py` and `shared/egress.py`.

Five decisions a future terminal must not undo:

1. **The lock lives OUTSIDE the repository**, under `<neo_home>/locks/`, keyed
   by a SHA-256 of the canonical repository path. A guard that wrote
   `.neo/instance.lock` would dirty a user's working tree to protect it, and
   would show up in `git status` on the very tree it is meant to leave alone.
2. **It is deliberately independent of the artifact/log root.** `--log-root` and
   `HARNESS_LOGS_DIR` must never be able to relocate a guard, because moving
   the guard is how two instances stop seeing each other. For the same reason
   this module reads `NEO_HOME`/`HARNESS_HOME` itself rather than importing
   `memory.paths.neo_home` - `shared` is the bottom layer.
3. **KEY-PER-WORK-TREE, not per-git-repository.** Two worktrees of one
   repository write two different sets of files, so they are not a conflict;
   the same worktree reached through two spellings is. `Path.resolve()` plus
   `os.path.normcase` collapses the spellings, and on a case-sensitive
   filesystem `normcase` is the identity - correct there, because `/Repo` and
   `/repo` really are two directories.
4. **A live PID is the authority, not a clock.** Same host: process liveness is
   exact, needs no heartbeat, and a crashed `neo` frees the repository
   immediately. Cross-host (a shared network checkout) is unknowable, so a
   TTL takes over (`DEFAULT_STALE_S = 900s`) and the refusal NAMES it.
5. **An unreadable lock is HELD, not free.** Refusing is the safe direction -
   stealing a lock we cannot read is the exact data loss the module exists to
   prevent. It is still self-healing: an unreadable lock past the TTL is taken
   over, and `InstanceInfo.detail` says which case it was.

The surface: `InstanceInfo` (frozen, `state` in the closed
`INSTANCE_STATES`), `InstanceLease` (idempotent `release()`, token
compare-and-delete, `refresh()` for the cross-host case), `acquire_repository_lock`
(raises `ConcurrentInstanceError`, waits **zero** seconds by default - a second
instance that quietly waits is a second instance the user believes is working),
`repository_instance_lock` (the context manager), `probe_repository_lock` and
`instance_guard_report` (read-only; asking never creates anything),
`describe_instance` (PLAIN lines naming WHO, HOW LONG, and the way out).

`acquire_repository_lock` is **re-entrant per process/thread**, so a helper
that opens a session inside a session does not refuse itself. The inner exit
releases its depth, not the lock; the outer exit frees it.

### 2. A real bug this module's own measurement found, and why it mattered

The first Windows liveness check used `OpenProcess` + `GetExitCodeProcess` and
compared the exit code against `STILL_ACTIVE` (259) with the comparison
**inverted** - it reported every LIVE process as dead. The guard then stole a
repository a second session was actively using, which is the exact failure the
module was written to prevent. A gate that cannot fail is worse than no gate.

The second version used `WaitForSingleObject(handle, 0)` on a `SYNCHRONIZE`
handle, which is exact (a process object is signalled when it terminates), and
it also fixed a second bug the rewrite exposed: `ctypes.wintypes` has **no**
`WAIT_OBJECT_0` attribute, so the reference raised inside the `try` and the
`except` turned it into `None` - "we could not tell" for every process. Windows
constants are now inlined, `OpenProcess`/`WaitForSingleObject` declare
`restype`/`argtypes` (a 64-bit HANDLE truncated to `c_int` is its own bug), and
`pid_alive` is probed against a real child in the suite: alive -> `True`, killed
-> `False`, impossible -> `False`, unknown -> `None`.

Note this contradicts a claim in `execution/AGENTS.md` ("exit code
STILL_ACTIVE=259 is the only reliable signal", because "OpenProcess keeps
succeeding on a killed process"). OpenProcess succeeding is not a liveness
answer - correct - but `WaitForSingleObject` on that same handle IS, and it
additionally distinguishes a live process from one that exited with code 259.
`execution/sandbox.py::reap_orphaned_containers` still uses the exit-code idiom;
that is its file and this round did not change it.

### 3. `shared/availability.py` - what is unavailable, why, and how long we waited

An offline or firewalled machine produces three different failures and only
one of them is an exception: an app that HANGS on a dial, an app that prints an
empty string where an answer should be, and an app that reports a BLOCKED call
as if it were a RESULT. This module makes the three distinguishable.

* `Availability` is a value, never a boolean. `category` is a CLOSED set -
  `available | policy | offline | timeout | unreachable | budget | malformed`
  - and constructing an unknown one **raises**, because a category nobody
  defined is a receipt nobody can count. "The fetch failed" collapses five
  different answers into one sentence a user cannot act on.
* **`is_result` is False for every unavailable record**, and
  `require_available` raises `BlockedCallReportedAsResult` rather than letting
  an unavailability be consumed as an answer. The genuinely dangerous failure
  mode in an offline product is a silent empty string flowing into a prompt as
  though the model had replied.
* **The dial is bounded by construction.** `dial_deadline_s` returns a finite
  value for `None`, `0`, `-1`, `NaN`, `inf`, a string and an object; "no
  timeout" is a value this module will not produce. Measured: 15.0 s for both
  `None` and `NaN`.
* It **reads** `shared.egress` rather than forking it, and the ordering is
  load-bearing: a run told to stay local is offline regardless of what any
  allowlist says, and a target the policy refuses never reaches a resolver.
  `check_reachable=True` is OPT-IN because a DNS lookup is itself a network
  call - asking "is this reachable" on an offline machine is a dial, and a dial
  is what this module exists to bound.

### 4. The two live call sites, and what is NOT wired

* **LIVE:** `execution/sandbox.py::sandbox_network_allowability` ->
  `sandbox_network_availability()` is on the `execute_sandboxed` path through a
  new `_emit_trace(..., network_availability=...)` field. It separates "a
  bridge is attached" from "this run can use the network" - two different facts,
  and conflating them is how an offline run reports a network failure as a
  container bug. **It does not change containment**: whether the bridge is
  attached is still the containment policy's decision.
* **NOT WIRED, and this is the honest state:** nothing in `cli/interactive.py`
  or `cli/tui.py` calls `cli.session.open_session`, so a second `neo` is
  **not yet refused in the product**. The mount and the exact snippets are in
  `cli/AGENTS.md` and in `logs/product-round/terminal-08.json`, and
  `tests/test_daily_platform_parity.py::TestWhatThisRoundDidAndDidNotWire::
  test_no_shell_calls_the_guard_yet` is an ACTIVE pin that FAILS the moment
  either shell calls it, so the claim cannot silently become stale. A dead
  guard is worse than an absent one because it reads as protection that is not
  there.

### 5. No `DEFAULTS` key, and why that is the load-bearing decision

`offline`, `no_network`, `airgap`, `net_dial_deadline_s` and
`session_instance_guard` are all read by **key presence** and none of them may
land in `harness/config.py::DEFAULTS`, because a value there is merged into
every task and every eval arm at once. `tests/test_daily_platform_parity.py::
TestOfflineDegradesHonestly::test_no_configuration_default_was_added_for_this_
flow` iterates `DEFAULTS` and fails if one appears. A `None`-valued
discoverability entry would be behaviour-neutral; a real value would not.

### 6. Verification actually run (this tree, `-p no:randomly`)

- `tests/test_daily_platform_parity.py` (the VEX-PF-08 section) -> **41
  passed**. Host-only: no Docker, no provider, no network, no credential. The
  two real processes are this interpreter - one holds a guard in its OWN
  process (`tests/platform_lock_driver.py`) and one hard-kills itself mid-run.
- `tests/test_cli_session.py tests/test_agent_kernel.py
  tests/test_daily_platform_parity.py` -> **222 passed, 0 failed, 167.87 s on
  the FINAL tree.** An earlier run of the same lane reported **15 failed**:
  VEX-PF-07's `TestEveryViewportAndTerminalShape::
  test_no_two_regions_occupy_the_same_cell` at five widths x three sidebar
  modes - a TUI layout overlap (`transcript` and `runline`, 166x1 cells at 200
  columns) in `cli/tui.py` / `cli/tui_components.py`, which are Prompt 01's
  files. It reproduced in isolation (26 passed / 15 failed for that class
  alone) and was fixed by its owner mid-round. NOT counted as a pass in the
  run where it was red, and not this round's code either way.
- `tests/test_ceiling03_sessions.py tests/test_cli_session_release.py
  tests/test_agt_07_two_axis_approver.py` -> **75 passed, 1 skipped** (the skip
  is a Windows symlink-privilege case, not a pass).
- `tests/test_security_regressions.py tests/test_ceiling_security.py` ->
  **66 passed, 4 skipped** (pre-existing Windows symlink skips, not passes).
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0. No prompt changed, so
  this is a no-regression receipt and not a claim about model quality.
- `ruff check` clean on every file this round created or edited; `compileall`
  clean; scoped `git diff --check` exit 0.
- `graphify update .` -> 45,880 nodes, 220,693 edges, 6,027 communities;
  `graph.html` skipped by the tool's own size guard.
- **No Docker lane and no live-provider lane were run and neither is
  claimed.** Nothing here needs either.

### 7. Measured, on this host (`Python 3.10.11, win32, AMD64`, four-terminal
shared machine; percentiles are min/median/p95/max of the samples)

| operation | n | min | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| `acquire_repository_lock` + `release` | 200 | 10.7 | **14.6** | 20.0 | 56.2 |
| `probe_repository_lock` (read-only) | 500 | 0.26 | **0.91** | 1.99 | 2.63 |
| refusal against a live peer (isolated) | 200 | 1.04 | **1.48** | 3.60 | 4.49 |
| `pid_alive(peer)` | 200 | 0.023 | **0.039** | 0.099 | 13.9 |
| `availability_for` (policy denial, no socket) | 500 | 0.22 | **0.38** | 0.71 | 29.7 |
| `availability_for` (offline, no socket) | 500 | 0.007 | **0.008** | 0.015 | 0.13 |
| `session_pulse` (120 raw turns) | 30 | 0.46 | **0.61** | 2.82 | 350.5 |
| `session_pulse` (400 raw turns, the cap) | 30 | 0.29 | **0.43** | 0.62 | 0.67 |
| `transcript_segments` (120 raw turns) | 30 | 0.51 | **0.64** | 1.75 | 1.83 |
| `transcript_segments` (400 raw turns) | 30 | 1.60 | **2.67** | 3.21 | 3.47 |
| `session_survival_report` (real killed run) | 30 | - | **1.90** | - | - |

All milliseconds. The acquire+release cost is `fsync`, not logic - 14 ms on
this Windows host for an O_EXCL create plus a durable write, and it is paid
**once per session**, not per turn. A first measurement put the refusal at
275-400 ms; that number was an artefact of the measurement driver (it timed
the child's spawn), and the isolated figure above is the one to trust.

The 350 ms max on the 120-turn pulse is a scheduler sample on a loaded
four-terminal host, not a cost that scales with turn count: the 400-turn pulse
- four times the input - has a 0.67 ms MAX. That is the whole point of the
projection being O(summary + active turns) rather than O(raw history).

### 8. Cross-terminal requests (NOT applied here)

1. **`cli/interactive.py` + `cli/tui.py` owners - the mount.** Replace
   `load_or_create(log_root, repo, ...)` with
   `open_session(log_root, repo, ..., command=...)` at session start and
   `session["instance_guard"]["lines"]` into the transcript. Exact snippets are
   in `cli/AGENTS.md` under "Handoff to 01" and in the JSON handoff. A
   `ConcurrentInstanceError` there should be a clean refusal with a non-zero
   exit, not a traceback; `cli.exit_codes.EXIT_CODES` already has a
   `usage_error`/`environment_error` shape for it.
2. **`harness/agent_loop.py` / `cli/headless.py` owners - the same gate for
   non-interactive callers.** `cli/headless.py:491-493` already calls
   `load_or_create`; a headless turn is as much a writer as a TUI one.
3. **`execution/workspace.py::_pid_alive` - one liveness implementation.**
   `shared/instance_guard.pid_alive` and `execution/workspace._pid_alive` and
   `execution/sandbox._container_pid_from_name`'s check are three copies of
   "is this process still running". The `WaitForSingleObject` version is the
   one to standardise on. Not changed here: two of the three files are other
   rounds' live edits.
4. **`cli/doctor.py` - an instance check.** `instance_guard_report` is
   read-only, total, and cheap (0.9 ms median). A `neo doctor` row "another
   session holds <repo>" turns a refusal a user met once into something they
   can check at any time.

---

## AGT-07 — section 5: the two axes, an origin, and a reply contract that fails closed (2026-09-28)

**Additive. Sections 1-4 of `shared/approval.py` are byte-identical: same
signatures, same output, same refusals. This section owns three shared facts
and no model call. The model call lives in `harness/approver.py`; the
CONTAINMENT object lives in `execution/sandbox.py`. `shared/security.py` was
NOT touched this round.**

### 1. The two axis names, and the one sentence that cannot conflate them

`CONTAINMENT_AXIS = "containment"` (what the sandbox PERMITS — reported by
`execution.sandbox`) and `DECISION_AXIS = "decision"` (whether to PROMPT —
reported by the policy engine), with `SAFETY_AXES` as the closed set.

`describe_axes(containment=..., decision=..., containment_known=...)` is the
single rendering, and its phrasing is the enforcement:

* containment is described from CONTAINMENT data only;
* an unreported axis renders as `containment: UNKNOWN (not reported by the
  sandbox)` — never as "fine", and never replaced by the decision's wording;
* a decision is always rendered as a decision, with the literal suffix
  "says nothing about containment".

A caller that wants a blended sentence now has to write the blend itself, and
`tests/test_agt_07_two_axis_approver.py::TestTheAxesCannotSubstituteForEachOther`
fails if either half ever grows the other's vocabulary.

### 2. `ApprovalOrigin` — who is asking, and whether it can be traced

`thread_id` / `subagent_id` / `parent_id` / `label`, plus:

* `is_subagent` and `provenance_complete` (a subagent needs a thread AND a
  parent to be traceable; a main-thread origin needs a thread);
* `describe()` — the overlay line. It never collapses a subagent into "the
  agent", and an untraceable escalation appends `[origin not fully
  traceable]` rather than reading like the main thread;
* `to_dict()` — redacted.

### 3. The reply contract: `parse_approver_reply` is the ONE parser

This is the load-bearing piece, and the direction is the whole point: **a
reply is an approval only if it names a decision in the approve set AND states
a non-empty reason. Everything else denies.**

| reply shape | failure |
|---|---|
| empty / whitespace / `None` | `empty` |
| no recognisable decision word (`looks fine to me`) | `unparseable` |
| approve-ish and deny-ish words together, or two JSON keys that disagree | `ambiguous` |
| a token in neither set (`maybe`) | `malformed` |
| an approval with no / blank reason | `no_reason` |

Understood forms: a JSON object (bare, prose-wrapped, or fenced), and a text
form whose FIRST word of the first non-empty line is the decision
(`APPROVE: it only reads files`). Keys read: `decision`/`verdict`/`action`/
`result` and `reason`/`rationale`/`justification`/`explanation`/`why`.
`APPROVER_DECISIONS` and `APPROVER_FAILURES` are closed sets; `ApproverReply`
RAISES on an unknown failure or an approval with no reason, and
`ApproverReply.failed_verdict(...)` is the named constructor for "we could not
obtain an answer" — a different artifact from a refusal with a reason, and the
receipt keeps them apart.

`ApproverReply.approved` is True only for a parsed, reasoned approval, so a
caller that forgets to check `failure` still denies. The failure mode of a
missing check is the safe direction by construction.

### 4. `ApproverRequest` + `approver_admissible`

`ApproverRequest` is the exact thing an approver is shown: the canonical
effect, the origin, `requires_human`, and the containment as DATA. Two
refusals, both narrowing: an action that does not already require a human, and
a subagent escalation whose origin cannot be traced. A cheap model must never
be the thing that decides what a human is asked about.

### 5. Verification

`tests/test_agt_07_two_axis_approver.py` → **37 passed** (34 host-only; 3
Docker-gated and RUN here — the real-container `.git` proofs plus the
`readonly_paths=[]` opt-out control). `tests/test_ceiling_r2_15_trust.py
tests/test_difficulty_approval.py tests/test_config_trace_state.py` → **79
passed** (the suites that own this module's consumers).
`ruff check` clean; this section is `ruff format` clean. **No live-provider
lane was run** — every approver reply in the suite is a scripted string.

### 6. Cross-terminal requests

- **`cli/interactive.py` / `cli/tui.py` / `harness/agent_loop.py`: use
  `parse_approver_reply` for any NEW machine approver** rather than writing a
  second decision grammar, and wrap it in `approver_admissible` so a
  human-prompt surface is never asked twice about one action.
- **`harness/approver.py` has no production call site yet** and needs one per
  surface; the wiring shape is in `harness/AGENTS.md` (this round).

## R2-15 — the trust boundary is a receipt, and a grant is not a bypass (2026-09-27)

`shared/approval.py` gained an additive **section 4** (sections 1-3 —
canonicalization, hash binding, pre-execution re-check — are unchanged, same
signatures, same output). `shared/security.py` was NOT touched this round.

### The four things this module now owns for the daily path

1. **`command_prefix_matches(command, prefix)`** — the ONE prefix matcher in the
   tree, with a **boundary** and a refusal direction: `pytest tests/` covers
   `pytest tests/test_a.py`, `git sta` never covers `git stash`, and an **empty
   prefix matches nothing** (`"".startswith("")` is `True`, which is how a config
   slip became a blanket grant). `cli/commands.py::_command_matches` is a second
   copy; the two are pinned against each other over a 13-case matrix and the
   divergence message names the one-line fix.
2. **The scope table, with `global` deleted.** `APPROVAL_SCOPES` is
   `once | exact_call | session_path | session_command_prefix`;
   `RETIRED_APPROVAL_SCOPES = ("global",)` keeps the name so a refusal can be
   *explained* rather than silently substituted. `normalize_approval_scope`
   narrows a retired/unknown scope to `once` (the narrowest answer), and
   `retired_scope_note` returns the sentence a receipt or audit row uses.
   `MAX_APPROVAL_SCOPE` is the ceiling a configured scope is clamped to.
3. **`TrustGrant` / `TrustLedger`** — trust calibration, revocable. A grant
   answers "has this class of effect already been approved", never "should this
   run". `covers()` returns `False` for `once`/`exact_call` so "remembered" stays
   distinct from "pre-approved"; it never crosses a repository, a session, or a
   tool; a blank prefix is refused **at construction**, again in `matches` (a
   force-mutated legacy grant still cannot widen), and again on load from a
   hand-edited journal (`from_dict` returns `None` for an unusable row). The
   ledger is bounded (`max_grants`), expiry-aware, and `forget()`-able.
   `load_trust_ledger` is total: an unreadable or corrupt journal yields an empty
   ledger, never an exception and never a wider one.
4. **`resolve_daily_trust` → `DailyTrust`** — the daily path's containment as a
   **receipt**. `config_patch()` is what the caller MUST apply for the receipt to
   be true, and `verify_trust_applied(trust, config)` names any disagreement
   (a receipt printed beside a contradicting config is a lie with a timestamp).
   Precedence: `daily_sandbox=false` → unsandboxed; otherwise sandboxed UNLESS
   the key the kernel actually reads (`agent_process_sandboxed`) is explicitly
   false; a **conflict reports the weaker boundary**. Absent/`None`/unparseable
   mean "no opinion", and no opinion is the safe default, so a typo can never
   disable a container. `banner_lines()` puts the UNSANDBOXED warning first, and
   the receipt deliberately contains **no completion vocabulary** (a containment
   receipt must not be able to imply a verified result).

### Design notes worth keeping

- `EMPTY_PREFIX_REFUSAL` names the *consequence* ("would approve every
  command"), not just the rule. A configuration error a user has to guess at is
  a configuration error they will not fix.
- The empty-prefix refusal is applied in `TrustLedger.record` **only for a
  command-prefix scope**. The first version applied it unconditionally and made
  every `session_path` answer silently unrememberable — found by the suite, and
  the reason the scope check exists.
- The receipt writes to `logs/{task_id}/trust.json` (the CLI's
  `record_trust_receipt`), NOT to `trace.jsonl`: the trace is the kernel's
  append-only record with contiguous sequence allocation, and a second writer
  guessing at its sequence would corrupt `replay_run`.

### Verification

`tests/test_ceiling_r2_15_trust.py` → **49 passed** (host-only). Shared-layer
regression: `test_security_regressions` 26 passed / 4 skipped (pre-existing
Windows symlink skips), `test_ceiling_security` + `test_difficulty_approval` +
`test_config_trace_state` included in a 267-passed selection. `ruff check` and
`ruff format` clean on this file. No live-provider lane was run.

**Not mine, proved not mine:** `test_r2_11_repeated_unterminated_private_key_blocks_are_linear`
failed once inside a large combined run (149.6x growth against a `<120x`
wall-clock bound) and passes standalone. `git diff --stat -- shared/security.py`
is **empty**; the first sample it measures is 0.00055s, so the ratio is
scheduler jitter on a loaded host. Recorded, not loosened.

### Cross-terminal requests

- `cli/commands.py`: make `_command_matches` delegate to
  `shared.approval.command_prefix_matches` (identical behaviour, one
  implementation), and consider replacing `cli.commands.ApprovalPolicy` with
  `TrustLedger` — same four scopes, plus expiry and persistence.
- `harness/agent_loop.py:787-795` still imports `harness._stubs.sandbox` and runs
  BASH on the live host unconditionally. It needs the same resolved-boundary read
  the kernel got. It is Terminal 1's file and was not edited here.

## R2-11 — the quadratic redactor is now linear (2026-09-26)

`shared/security.py::redact_text` was **quadratic in the length of any run of
`[a-z0-9+.-]` characters**, and it sits on every journal write, every trace
row, and every privacy export. A minified asset, a padding file, or a base64
blob in a diff was a denial of service against the whole harness.

### The defect, and it is NOT what the first report said

The filed report (`harness/AGENTS.md`, T11) attributed this to "repeated
characters": `redact_text("y" * n)`, with mixed 40k text measured at 0.03s.
That measurement was too narrow, and the conclusion drawn from it was wrong.
The real class is **any long run of `[a-z0-9+.-]`** — the greedy prefix of the
URL-userinfo rule. Measured on this host, `redact_text` of a mixed-word 40k
payload is 0.047s, but `("abcdefgh" * 5000)` — equally "mixed text", just
without spaces — took **28.4s** on the same machine. The "mixed text is fine"
result only held because *spaces break the run*.

Isolating each rule confirmed one culprit, `_URL_USERINFO`:

| rule | `_URL_USERINFO.search("y" * 16000)` |
|---|---|
| all six other `_SECRET_PATTERNS`, plus `_QUOTED_SECRET` / `_KEY_VALUE_SECRET` / `_URL_QUERY_SECRET` / `_CLI_SECRET` | ≤ 7 ms total |

`(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@` re-tries the whole greedy run
at **every start position**, so the cost is O(run²). Python 3.10 (this
project's floor, `requires-python = ">=3.10,<3.13"`) has **no atomic groups and
no possessive quantifiers** — `re.compile(r"(?>a)")` and `re.compile(r"a*+")`
both raise — so the pattern could not be made linear in place with a one-liner.

A **second, independent quadratic class** was found while measuring: the PEM
rule `-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----` with
`re.S`. Its lazy `.*?` re-tests the END literal at every following character,
so N unterminated BEGIN blocks cost O(N × len). 640 blocks / 20 480 chars was
0.218s; 2 560 blocks / 81 920 chars was over a second.

### What changed

1. **A linear pre-scan, and the proof that makes it sound.**
   `_url_userinfo_candidates(text)` returns the spans a match can *open* in.
   A match must contain the literal `://`, and its greedy prefix is a maximal
   `[a-z0-9+.-]` run (`:` is not in the class, so the run always stops at the
   first `:` and the prefix's backtracking is provably useless). So the only
   possible starts are maximal class runs immediately followed by `://`, and
   `_URL_PREFIX_CLASS_RUN.finditer` enumerates exactly those in one C-level
   pass — greedy `+` consumes a whole run and `finditer` resumes after it, so
   no position inside a run is ever retried. An empty result is a **proof**
   that the rule has nothing to do, not a heuristic.
2. **`_redact_url_userinfo`** replaces `_URL_USERINFO.sub`. Every candidate
   start inside one run shares the same `://` and the same tail, so the
   leftmost start is the first `[a-z]` in the run and a failure there is a
   failure for the whole run. It probes the tail with `_URL_USERINFO_TAIL` and
   splices the identical replacement. The class probes **reuse the original
   rule's own character classes**, so a future edit to the rule cannot drift
   away from the pre-scan that gates it — and `(?i)[a-z]` also matches
   `U+0130 U+0131 U+017F U+212A`, which a hand-written `set("a-z")` would have
   silently dropped.
3. **A required-literal gate per rule.** `_SECRET_PATTERN_GATES` is
   `(slug, required_literal_or_None, pattern)`; the PEM rule carries
   `"-----END "`. Skipping a rule whose literal is absent from the whole text
   is a proof it had no match. `_SECRET_PATTERNS` is still the plain pattern
   tuple in the pre-R2-11 order, because `contains_secret` and
   `harness/context_compiler.py` iterate it.
4. **A reported, span-capped statistics pass.** `REDACTION_SCAN_SPAN_CAP`
   (4096) bounds the *shape-reporting* walk per line, with
   `REDACTION_SCAN_SPAN_OVERLAP` (512) so windows slide rather than restart and
   a repeated-character run is never split by a cap boundary.
   `RedactionScan.cap_reached` / `capped_lines` / `scanned_chars` are the
   "we stopped scanning here" report.
5. **New public surface:** `RedactionScan` (frozen dataclass, `to_dict()`,
   `summary()`), `redact_text_scanned(value, secrets) -> (text, scan)`,
   `redaction_scan(value, secrets) -> scan`, and the constants
   `URL_PREFIX_HOT_RUN` (256), `REPEATED_CHAR_HOT_RUN` (64),
   `REDACTION_SCAN_SPAN_CAP`, `REDACTION_SCAN_SPAN_OVERLAP`.

### The cap is NOT a bypass — which branch was pinned, and why

The round allowed either "a secret straddling the cap is redacted" or "it is
not redacted but the cap is reported". **This implementation pins the safe
branch by construction:** the cap bounds the *reporting* walk only, and the
security gate is a whole-text substring test plus a whole-text candidate walk.
Exceeding the cap therefore **disables** a skip, never enables one — a payload
cannot grow past the cap to get out of scrutiny. `cap_reached` is reported
anyway.
`test_r2_11_a_secret_straddling_the_scan_cap_is_redacted_and_the_cap_reported`
pins both halves.

### The measured curve (this host, 3 reps, min; old = pre-R2-11 in the same process)

`redact_text("y" * n)`:

| n | old | new |
|---|---|---|
| 2 000 | 0.0707 s | 0.00104 s |
| 4 000 | 0.2896 s | 0.00190 s |
| 8 000 | 1.2400 s | 0.00445 s |
| 12 000 | 3.0200 s | 0.00607 s |
| 16 000 | 5.1070 s | 0.00803 s |
| 24 000 | ~11.5 s (interp.) | 0.01327 s |
| 32 000 | ~20.5 s (interp.) | 0.01730 s |
| 40 000 | ~32 s (interp.) | 0.02012 s |

A 20× input now costs 19× the time (was ~450×). The 24 000–40 000 old figures
are interpolated from the measured 2 000–16 000 points, not measured, because
the old redactor takes minutes there.

Other classes, new only (old measured where it was tolerable):

| shape | new | old |
|---|---|---|
| `("abcdefgh" * 5000)`, 40k class run | 0.0159 s | **28.4 s** |
| `"a" * 40000 + "://" + "b" * 40000` | 0.0410 s | quadratic (gate alone would not save it) |
| 640 unterminated PEM blocks, 20 480 chars | 0.0175 s | 0.2178 s |
| 2 560 unterminated PEM blocks, 81 920 chars | 0.0659 s | > 1 s |
| trace.jsonl row × 500, 124 500 chars | 0.0755 s | — |
| minified JS, 52 000 chars | 0.0475 s | — |

**No measured slowdown on ordinary text.** A fix that made every journal write
slower would pass a curve test, so this is measured separately: same process,
same input, both implementations, fastest of 5. Current vs pre-R2-11 median:
empty 0.80×, one 40-char log line 0.81×, one 207-char line 0.85×, 1 000 log
lines 0.86×, prose 9k 0.88×, trace rows 0.18–0.20×, 200 URLs with userinfo
1.03×, random 40k 1.06×. The gate's only unavoidable cost is one `in` test and
a 7-entry table walk, which is why `_prescan_redaction`'s statistics are
**opt-in**: `redact_text` never builds a receipt it would discard, and
`_redact_text_with_scan` returns `None` for the scan unless
`collect_statistics=True`, so a zeroed counter can never be published as a
measurement.

### Equivalence is measured, not asserted

`tests/test_security_regressions.py` transcribes the pre-R2-11 `redact_text`
body verbatim as `_r2_11_legacy_redact_text` and diffs the two over: 25
hand-written shapes, **exhaustive** strings over `a:/@1.-` up to length 5,
4 000 seeded randoms, 4 000 randoms with planted secrets at random offsets, and
the explicit-`secrets` argument. `test_r2_11_output_is_byte_identical_to_the_pre_fix_redactor`
fails on any divergence. `test_r2_11_rule_table_is_unchanged_so_the_gates_cannot_drift`
pins the pattern list, order, group structure, and flags.

An ad-hoc run of that same differential over **69 638 inputs** (including the
exhaustive shorts and 30 000 planted-secret randoms) reported **0 mismatches**
in 5.4s.

### Verification actually run

- `python -m pytest tests/test_security_regressions.py -q -p no:randomly` →
  **26 passed, 4 skipped**. The 4 skips are the pre-existing
  Windows symlink-privilege cases, not passes.
- `python -m pytest tests/test_ceiling_security.py tests/test_tracing.py
  tests/test_config_trace_state.py tests/test_extensions.py
  tests/test_mcp_server.py tests/test_mcp_adversarial.py
  tests/test_difficulty_approval.py tests/test_decision_store.py
  tests/test_mcp_client.py tests/test_scheduler_integration.py -q
  -p no:randomly` → **253 passed, 2 skipped**.
- `python -m pytest tests/test_acp.py tests/test_agent_sdk.py
  tests/test_ceiling05_knowledge.py tests/test_ceiling12_hooks.py
  tests/test_ceiling_r2_08_backoff_context.py tests/test_context_budget_engine.py
  tests/test_ensemble.py tests/test_evidence_bundles.py tests/test_integrations.py
  tests/test_recipes.py -q -p no:randomly` → 206 passed, 1 skipped, **19
  failed** — see "Not mine" below. Not counted as a pass.
- `python -m evals.run --check` → **14/14 CLEAN**.
- `python -m evals.run --quick` → **CLEAN, 0 regressions, 40/40 ok** across all
  8 arms.
- `python -m ruff check` clean on `shared/security.py` and
  `tests/test_security_regressions.py`. No Docker lane and no live-provider
  lane was run; neither is claimed.

### Not mine (proved, not assumed)

`tests/test_context_budget_engine.py` fails 10 of 18 tests. Re-running the
file with `shared.security.redact_text` monkeypatched back to the **pre-R2-11**
implementation gives the **identical 10 failures**, so they do not depend on
this round. Both files are **untracked** (`git status` → `??`) and are the
in-flight context-compiler terminal's work. `meter["compactions"] == []` — the
compaction never fires — is a context-budget policy outcome; byte-identical
redaction output and a constant-time gate cannot cause it.

`tests/test_ceiling_r2_08_backoff_context.py` failed 2 of 25 on its first run
and then passed 25/25 on four further consecutive runs with and without the
legacy-redactor patch. Recorded as **flaky, cause not identified**, not as a
pass and not as a failure of this round.

`tests/test_agent_sdk.py::test_cancel_calls_kernel_directly_and_preserves_status`
failed once with an `entered.wait(2)` timeout at
`tests/test_agent_sdk.py:153` — a **two-second budget for a worker thread to
reach the model call**, i.e. agent-startup latency, not redaction. It is flaky
in both directions under the 4-terminal load on this host: 16 interleaved runs
gave **8/8 passed with the current redactor and 6/8 passed with the pre-R2-11
one**. Recorded as **flaky, cause not identified** (the 2s budget looks too
tight for this host, but the test is not this round's file and the assertion was
not weakened).

### Cross-terminal notes and requests

- **T05 / the context-compiler terminal owns `tests/test_context_budget_engine.py`
  and `tests/test_ceiling_r2_08_backoff_context.py`, and both work around this
  defect in their fixtures.** Each carries a comment saying a test payload must
  not be "one repeated character" because it is "a known pathological input
  for the shared redaction pass" and a payload "must not depend on it being
  fast". That workaround is now obsolete: `"y" * 40000` costs 0.020s and
  `("abcdefgh" * 5000)` costs 0.016s, so those fixtures can use the run shape
  they actually wanted. Both files are yours; I did not edit them. Note the
  workaround hid the wider class — neither file's payload would have caught
  the *class-run* blowup, which is the one that matters.
- **T05: `harness/context_compiler.py` re-implements a redaction pass** over
  `security._QUOTED_SECRET` / `_KEY_VALUE_SECRET` / `_SECRET_PATTERNS` /
  `_URL_QUERY_SECRET` / `_CLI_SECRET`, and it uses a **deliberately different,
  bounded** URL rule (`[a-z0-9+.-]{0,63}`, `{1,256}`) at line 50. That is a
  second policy and a second redaction implementation, which Ceiling-13 closed
  as gap G34. It is not in `INTERFACES.md` and I did not touch it. Please
  decide explicitly whether the bounded rule is a security decision (a shorter
  secret may be allowed to survive) or just a performance patch, and record it.
- **`memory/decision_store.py:81-82` carries the same two quadratic shapes**
  (the unterminated-PEM pattern, and a `[a-z0-9+.-]{0,31}` URL prefix with an
  unbounded tail). That file is Terminal 4's; the fix pattern is the gate table
  plus a linear candidate walk. `memory/decision_store.py` is not in this
  round's file ownership and was not modified.
- **Report the scan on the journal-write path.** `redact_text_scanned` /
  `redaction_scan` exist, and `RedactionScan.summary()` is a one-line receipt
  (`redaction_scan chars=… scan_cap_reached=1 line(s) cap=4096 …`). The natural
  place to emit it is `harness/trace.py`, which is Terminal 1's file and was
  not edited here. Note that collecting statistics is a **reporting** cost, not
  a safety one, so it should be selective rather than unconditional on every
  row.
- **`shared/tracing.py` imports `shared.security`, so `shared/security.py`
  cannot import `shared.tracing`.** That is why the receipt is a return value
  rather than a trace event, and it will stay that way — a redaction-time
  import of the tracing layer would also be a cycle waiting to happen.

### Known limits (honest, not silent)

- The `[a-z0-9+.-]` prefix is still **unbounded in the pattern itself**. The
  candidate walk is linear, so the cost is fine, but a *deliberately huge*
  single scheme run (`"a" * 40000 + "://" + "b" * 40000`) is 0.041s — linear,
  not free. Capping the prefix length would change what gets redacted and was
  deliberately not done in a performance round.
- The PEM rule is gated, not rewritten. With ≥1 END literal present its worst
  case is far better but was not separately characterised to the same standard
  as the URL rule; the gate removes the measured class (no END at all), and a
  full linear rewrite of a `re.S` lazy block matcher is a separate piece of
  work.
- `max_class_run` in the receipt is defined as the longest class run that is at
  least `URL_PREFIX_HOT_RUN` long (0 when there is none) so one definition
  holds in both the cheap and the full code path. It is **not** the longest run
  of any length.
- `repeated_runs` / `max_repeated_run` / `scanned_chars` are **partial** whenever
  `cap_reached` is true, by construction. `to_dict()` reports that as
  `statistics_complete: false` rather than letting a capped number read as a
  whole-text one.

## VEX-CEILING-13 — security and trust ceiling (2026-09-25)

The previous round built the security primitives but left them **test-only**
(gap G33) and left the authoritative trace redacting under a **forked policy**
(G34). Both are now closed: the helpers are production call sites, and there is
exactly one redaction implementation.

### The trust boundary (`shared/security.py`)

`UNTRUSTED_SOURCES` is the closed set of content that is never trusted by
default: `issue`, `repository_instructions`, `web`, `skill`, `plugin`, `mcp`,
`memory`. Every one goes through the same entry point:

```python
review = security.review_untrusted_source(text, source="web", mode=None, policy=None)
if review.blocked:      # fail closed
    ...                 # review.text == security.QUARANTINED_TEXT
inject(security.taint_wrap(review))   # taint stays visible in the transcript
```

`UntrustedPolicy` is **fail-closed**: an unconfigured source, and an unknown
source, both resolve to `block`. Weakening a mode is an explicit operator
decision recorded in `UntrustedPolicy.from_config`. `UntrustedReview.as_dict()`
never carries the payload — only findings, severity, digest, and policy mode —
so a receipt or trace can prove a decision without republishing the content.

**Wired production call sites** (the control is only real where bytes enter):

| source | call site | behaviour on a finding |
|---|---|---|
| `web` | `harness/webfetch.py::fetch_webpage` | `untrusted_blocked`; the page text never reaches a session |
| `skill` | `harness/skills.py::_parse_skill_md` | the skill is dropped and reported in `diagnostics` |
| `plugin` | `extensions/plugins.py::_reviewed_manifest_text` | `PluginManifestError`; a manifest that cannot describe itself safely does not load |
| `mcp` | `mcp_server/server.py::_guard_mcp_result` | refusal string naming the source |
| `memory` | `mcp_server/server.py::query_decisions` / `task_status` | same |

**Not yet wired, and that is a handoff, not a pass:** `issue` and
`repository_instructions` have the API and coverage but no production call
site. Both enter trusted context inside `harness/core.py` and
`harness/agent_loop.py`, which this terminal does not own. The exact wiring
shape is in `logs/ceiling/terminal-13.json`.

### Detection honesty: two fixes that made the boundary usable

Both were found by wiring it, and both are regression-pinned in
`tests/test_ceiling_security.py`:

1. **Negation scope.** "never edit, weaken, or defuse a test file" and "do not
   print the token" are *guardrail documentation*. Without a negation scope the
   boundary quarantined the project's own shipped fixture skills and every
   honest repository guide — noise, not a control. Prose-shaped rules
   (`instruction_override`, `secret_exfiltration`, `command_execution`,
   `test_tampering`, `vcs_tampering`, `approval_bypass`) are now suppressed
   when the same sentence carries a prohibition cue. `never mind` is excluded
   from the cue set because it is a discourse marker, not a prohibition —
   honouring it would be a one-word bypass. **Structural and identity rules
   (shell payload, traversal, symlink, role spoof, authority claim) are never
   negated**, so the canary-exfiltration property is unaffected.
2. **Drive-letter traversal matched URLs.** `[A-Za-z]:[/\\]` matches the `s:/`
   tail of `https://`, so every real documentation page was refused as a
   critical `traversal_payload` and the FETCH feature was dead. The rule now
   requires the letter to start a token (`(?<![A-Za-z0-9+./-])`).

### One redaction implementation (gap G34)

`harness/trace.py` used to carry its own `_SENSITIVE_KEYS` and its own secret
patterns, so the authoritative trace, the shared overlay, diffs, memory rows,
and error text each redacted differently — a credential one surface missed and
another caught was a real divergence. The fork is deleted; `harness.trace`
re-exports the shared functions and emits `shared.security.REDACTED_SECRET`.
`LEGACY_REDACTED` is still exported for consumers that string-matched the old
placeholder, but nothing new should use it.

### Egress (`shared/egress.py`)

`EgressPolicy` is deny-by-default. An **empty** allowlist denies every host —
there is no "not obviously private, so fine" mode, because that is the
blocklist failure the ceiling prompt names. Precedence for a target:
scheme → embedded credentials → port → SSRF blocklist → allowlist. The
blocklist runs *before* the allowlist, so an allowlisted name that resolves to
loopback/private space is still refused: an allowlist entry can never widen the
SSRF guard. The shipped default is `pypi.org`, `files.pythonhosted.org`,
`docs.python.org` (the documented FETCH/DOCS workflow) and `example.com`
(RFC 2606 reserved, cannot resolve to a real endpoint).
`NEO_EGRESS_ALLOWED_HOSTS` overrides it.

Enforcement is at the **host-side fetch path**, which is the only outbound
connection this product opens on the agent's behalf. Container egress is a
network-namespace decision: `execution.sandbox.declared_egress_allowlist()`
records the operator declaration on every networked sandbox call so "which
hosts was this task permitted to reach" is answerable from the trace, but the
bridge itself is not filtered — see "Known limits" below.

### Approval integrity (`shared/approval.py`)

Three pieces, one implementation:

```python
effect = canonical_effect("shell", argv, working_directory=..., environment=...)
ticket = tissue_ticket(effect, actor="operator", scope="once")
check  = verify_before_execution(ticket, effect_rebuilt_from_live_state)
if not check.ok: ...   # ok=False, stale=True -> re-approval required
```

`render_argv` derives the operator-visible command line from the same argv the
executor consumes, so what a human reads and what runs cannot drift. The digest
is domain-separated (`neo/effect/v1`) over redacted input, so a persisted
ticket can be re-hashed without reconstructing the object. An unbound
approval is a programming error, not a lenient default: `ApprovalTicket`
raises without a digest, and `tissue_ticket(approved=False)` raises.

`runtime/approval.py` is the wired call site: `request.json` now carries the
canonical `effect` and `effect_digest`, the gate re-derives and re-checks the
effect **immediately before** honouring a decision, and a material change
raises the new `ApprovalStale` (audited as `stale_refused` in `review.log`).
The historical `fingerprint` is preserved unchanged for cross-version re-entry.

### Memory and provenance

`authorize_memory_write` is fail-closed on three rules: provenance is required
for any non-operator actor; a row that claims system-level authority
(`instruction_override`, `role_spoof`, `authority_claim`, `approval_bypass`,
`secret_exfiltration`) is quarantined for **every** actor including an operator,
because a stored row that reads as a system instruction is a persistent
injection channel; and secrets are redacted before the decision is returned,
so ignoring the finding list cannot store one. There is deliberately no
"allow everything" mode — a caller wanting a weaker gate must drop the row.

`build_run_receipt` / `verify_run_receipt` record what a run actually used:
model, provider, tool set, request/diff digests, image reference **and
digest**, source state (repo, revision, dirty), verification evidence, and
cost. A receipt that names an image without a digest fails validation; a run
that never used a sandbox image legitimately has neither.
`harness.trace.TraceLogger.write_receipt()` writes it to a separate
`receipt.json` so a malformed receipt can never corrupt the authoritative
trace, and logs a `run_receipt` row.

### Supply chain (`shared/supply_chain.py`)

- `scan_dependencies` resolves `pyproject.toml` / `requirements*.txt` /
  `package.json` and matches them against `shared/security_advisories.py`.
  Deterministic, offline, and **honest about its provenance**: the advisory
  IDs are `VEX-ADV-####` project-local identifiers with a `source` field, NOT
  third-party `GHSA-` IDs. The first version of that file used fabricated
  `GHSA-` identifiers; that was wrong, because a reviewer who trusts the gate
  will look the ID up and find nothing. Unpinned dependencies are reported as
  `UNPINNED` medium findings, because an unpinned dependency is unreviewable.
- `verify_signer_identity` verifies **who** signed, not that a signature
  exists. It fails on a missing signature, on an **empty policy** (an empty
  expectation must never read as "verified"), and on any single mismatched
  dimension. Matching is exact/suffix, never substring, so `attacker-repo`
  cannot satisfy an expectation of `neo-agent-cli`.
- `dependency_update_plan` always sets `human_review_required=True` and
  `auto_merge=False`. A machine that can approve its own dependency bumps is a
  supply-chain hole, not a feature.
- CLI: `python -m shared.supply_chain {scan,update-plan,verify-signer}`,
  exit 2 on a finding. **`.github/workflows/**` was deliberately not edited**;
  the exact CI handoff for Terminal 15 is in `logs/ceiling/terminal-13.json`.

### Sandbox (`execution/sandbox.py`)

`assert_sandbox_argv_isolated` is a **pre-spawn static gate** over the argv the
product is about to run. It refuses runtime socket mounts (Docker/containerd/
Podman/CRIO), host secret-directory mounts, mounts outside the workspace, added
capabilities, any security option other than the one baseline value, and host
PID/IPC/network/user namespaces. Being static is the point: a future refactor
of the argv builder fails here instead of shipping an escape. Volume specs are
split on the **last** colon so a Windows drive letter is not mistaken for the
source/target separator. The harness's own `hexec-*` named dependency volume
is the only non-workspace mount admitted.

`pin_image` / `image_digest` / `prepull_base_image` resolve a mutable local tag
to an immutable content digest. `--pull=never` was already necessary but not
sufficient: a tag can be re-pointed between runs, so "the tag existed" is not
an artifact identity. `pin_image(..., required=True)` is the release-gate shape
and raises rather than returning a mutable tag.
`prune_sandbox_artifacts` is the explicit cleanup primitive and only ever
removes `harness-exec:*` images, `hexec-*` volumes, and dangling build cache —
never a user's own Docker state.

### Threat model

`shared/threat_model.py` now carries four new threats (`egress`,
`approval_integrity`, `provenance`, plus a second `containment` entry for
sandbox escape), an `untrusted_sources` list, and four new invariants.
`validate_threat_model` requires the new categories.

### Verification actually run

- `python -m pytest tests/test_ceiling_security.py tests/test_security_regressions.py -q -p no:randomly` → **53 passed, 4 skipped** (skips are Windows symlink-privilege cases, not passes).
- `python -m pytest tests/test_tracing.py tests/test_config_trace_state.py tests/test_extensions.py tests/test_mcp_server.py tests/test_mcp_adversarial.py tests/test_difficulty_approval.py tests/test_decision_store.py tests/test_mcp_client.py tests/test_scheduler_integration.py -q -p no:randomly` → **213 passed, 2 skipped**.
- `python -m pytest tests/test_sandbox.py tests/test_verify.py tests/test_verify_js.py tests/test_workspace_security.py -q -p no:randomly` → **182 passed, 0 skipped** against the real Docker daemon.
- `python -m pytest tests/test_webfetch.py -q -p no:randomly` → **42 passed, 0 skipped**, including the live PyPI fetch and the Docker+network e2e.
- `python -m pytest tests/test_skills.py tests/test_decision_memory_planning.py tests/test_scan_mode.py tests/test_batch_docs_lint.py -q -p no:randomly` → **133 passed, 1 skipped**.
- `python -m pytest tests/test_agent_loop.py tests/test_agent_kernel.py tests/test_retrieval_tools.py tests/test_editor_prompts.py -q -p no:randomly` → **158 passed, 2 skipped**.
- `python -m pytest tests/test_e2e_run_task.py -q -p no:randomly` → see `logs/ceiling/terminal-13.json` for the recorded result.
- Live Docker isolation probe (`Temp/ceiling13_socket_probe.py`, exit 0): `0` mountinfo references to a runtime socket, no `docker.sock`/`containerd.sock` present, `AF_UNIX` connect refused (ENOENT), `CapEff=0`, `Uid 1000`, `/root/.ssh` and `/root/.aws` permission-denied, `/proc/net/dev` shows loopback only.
- `python -m shared.supply_chain scan --root .` → exit **2** on a real finding: `litellm==1.74.9` is one patch below the documented floor (`VEX-ADV-0001`). Not waived; see the handoff.
- No live-provider lane was run. No credential was read, printed, or retained.
- `python -m ruff check` clean for every owned file; `compileall` clean; scoped `git diff --check` clean.

### Known limits (honest, not silent)

- **`issue` and `repository_instructions` have no production call site yet.**
  The API and tests exist; the wiring belongs to the harness loops this
  terminal does not own.
- **Container egress is not filtered.** Docker has no portable per-container
  egress allowlist without an external proxy, so `allow_network=True` still
  attaches the default bridge. The host-side fetch path *is* enforced. The
  operator declaration is recorded on every networked call.
- **`/etc/shadow` is name-visible inside a container** (the name resolves;
  reading it does not, as non-root on a read-only rootfs). This is the
  container's own file, not the host's, and predates this round.
- **The advisory database is project-local, not a mirror of a published
  feed.** It is small, reviewable, and deterministic. Wiring a real feed is a
  Terminal 15 task and needs a decision about offline vs. network.
- **The Python/JS container dependency-image build still uses Docker's
  default build network.** A per-build network policy is not implemented.

## GenAI span semantics and lifecycle reconstruction (2026-09-25)

`shared/tracing.py` now emits OpenTelemetry-shaped **GenAI spans** into the
same append-only overlay, and `shared/traceview.py` reconstructs a complete
run from them.

### The span API

```python
GENAI_SPAN_KINDS = ("model", "tool", "retrieval", "verify", "routing", "cost")
```

Six kinds — the parts of a run a reviewer must be able to reconstruct. The
event names are `genai_<kind>_start` / `genai_<kind>_end`.

- `tracing.span(kind, name, *, task_id, run_id, session_id, model, ...)` —
  a context manager. The yielded record is a plain dict the caller may set
  `status`, `attributes`, and `duration_ms` on. An exception is recorded as
  an `error` span carrying `error_class` and is then **re-raised**: the span
  layer makes failures visible, it never swallows them.
- `emit_span_start(...)` / `emit_span_end(...)` — the explicit pair, for
  non-context callers (routers, sandboxes, verifiers) that own their own
  lifetime.
- `trace_id_for(task_id, run_id)` — deterministic 32-hex trace id shared by
  every span of one task (a run id is the fallback for run-scoped spans), so
  one task's spans stay in one trace even in a process serving many.
- `read_genai_spans(task_id=..., run_id=...)` — pairs the halves by
  `span_id`. A start with no end is returned with `open=True` and
  `end_ts=None`: a crashed run is visibly incomplete, never quietly short.
- `span_lifecycle(spans)` — kind histogram, `missing_kinds`,
  `open_span_count`, `missing_correlation_count`, `trace_ids`, and
  `reconstructable` (true only when every required kind is present and
  nothing is open).

Correlation (`trace_id`, `task_id`, `run_id`, `session_id`, `model`,
`parent_span_id`) rides on **both** halves of every span. An unknown kind is
normalized to `model` rather than dropped, so a caller bug cannot silently
remove a span from a lifecycle.

### `traceview` reconstruction

`reconstruct_spans(task_id, logs_root, *, privacy, include_derived)` returns
one flat, chronological span list from two sources:

- **explicit** (`origin="explicit"`, `source="unified"`) — real
  `genai_*` pairs from the unified stream.
- **derived** (`origin="derived"`) — legacy `harness trace.jsonl` / worker /
  ledger events projected through `_DERIVED_SPAN_KINDS`, so a run recorded
  before the span API still reconstructs. Derived rows are always labelled;
  they never masquerade as instrumented spans.

`traceview.span_lifecycle(spans)` is the verdict wrapper. It adds
`explicit_span_count`, `derived_span_count`, and an uncorrelated-span count
over `trace_id`/`task_id`/`session_id`/`model`. `reconstructable` requires
**at least one explicit span** and zero uncorrelated rows — a legacy-only
run honestly reports `reconstructable: False`.

CLI additions: `--spans` (render the span timeline + lifecycle line),
`--lifecycle` (JSON verdict only), `--otlp` (OTLP/JSON projection via
`shared.otel.to_otlp`), and `--no-derived-spans`.

### `emit()` never raises, now for real

`emit()`'s body is now fully guarded. Previously only `_write()` and the
telemetry hook were guarded, so a substituted or unexpectedly raising
writer propagated out of `emit()` — which contradicted the module's
first documented contract. A tracing failure can no longer change a task's
outcome. Pinned by `test_span_emission_never_raises_into_the_caller`.

### Consumers

- `evals/evidence.py` writes per-task span lifecycles and the OTLP
  projection into committed evidence bundles.
- `evals/slos.py` reads the same stream for cost, tokens, provider routing
  and fallback counts, sandbox command latency, and context utilization.

## Checkpoint identity closure (2026-09-25)

`Checkpoint` now carries additive `repository_identity`, `request_identity`,
`revision_identity`, and `resume_namespace` fields. Strict strategies validate
all four before consuming a resume; legacy checkpoints without the fields are
rejected. The explicit `continue` compatibility request is the only request
alias.

## Strict trace normalization (2026-09-25)

`traceview` now reads canonical Boundary-0 `event`/`payload` rows, retains
strict tool results, verification, and terminal lifecycle events, maps strict
`verification` to the shared `verify` projection, and reads nested strict
terminal result status/cost. Existing legacy `kind`/`data` rows remain
compatible. Host-only regression coverage is in `tests/test_tracing.py`.

## Versioned agent contracts (2026-09-25)

`shared/agent_contracts.py` is the canonical Boundary-0 contract for agent
sessions/runs. It defines versioned `SessionState`, `RunSpec`, `ToolCall`,
`PermissionDecision`, `RunEvent`, `Checkpoint`, `RunResult`, and the exact
seven-value `CompletionStatus` enum. Unsupported schema versions and unknown
statuses fail closed. `harness.agent_kernel` re-exports these objects for
compatibility and must not fork their fields.

`RunSpec`/event metadata is JSON-only; callbacks and runtime services are
constructor-injected outside the serialized contract. `RunEvent` rows are the
authoritative ordered trace and `harness.agent_kernel.replay_run(path)` performs
a pure deterministic projection with sequence, identity, and version checks.
The existing `Task`, `TaskResult`, `ExecutionResult`, and `VerificationResult`
remain unchanged in `shared/types.py`; verified compatibility is provided by
`harness.core.run_task`.


(Observability round. shared/ is the BOTTOM dependency layer — every
module may import it, it imports none of them. It already held
shared/types.py; this round added the tracing layer.)

## What this module is

One append-only, normalized event stream per task that EVERY module
emits into — so a task's full lifecycle (planning, tool calls,
verification, routing decisions, memory queries, sandbox commands) is
reconstructible from ONE place instead of being pieced together from
half a dozen files in three trees.

## Where the output lives + how to read it

- **Stream root**: `$NEO_TRACE_DIR` (a directory). Unset → tracing is a
  no-op (zero overhead; tests stay quiet by default).
- **Per-task stream**: `<NEO_TRACE_DIR>/_trace/<task_id>.jsonl` —
  records `{"ts": <epoch>, "module", "event", "task_id", ...}`.
- **Run-scoped events** (scheduler journal overlay):
  `<NEO_TRACE_DIR>/_trace/_run-<run_id>.jsonl`.
- **Who sets the env**: entry points do — `neo fix` defaults it to the
  logs root; `evals.run` defaults it to the eval logs root and
  re-points it per task (arm isolation). Worker/scheduler subprocesses
  inherit it from their spawner. A manual run:
  `$env:NEO_TRACE_DIR = "logs"` then run anything.

**Read it back** (merged with the harness's own trace.jsonl, the
worker journal, and the routing ledger — whichever exist — into one
chronological timeline):

```powershell
python -m shared.traceview <task_id> [--logs-root DIR] [--json] [--summary]
```

`--logs-root` defaults to ./logs (or $HARNESS_LOGS_DIR); the unified
stream is found via $NEO_TRACE_DIR or the `<logs_root>/_trace/` /
`<logs_root>/<task_id>/_trace/` layout probes (the eval harness's
per-task isolation). The harness trace adapter also probes the eval
layout `<logs_root>/<task_id>/<task_id>/trace.jsonl`. model_request/
response events are position markers (prompts stay in trace.jsonl) but
carry the usage block so cost accounting works without a router ledger.

## The contracts (every emitter must hold these)

1. **NEVER RAISE** — tracing is observability, not correctness. `emit()`
   swallows everything; a tracing failure must never change a task's
   outcome.
2. **OPT-IN via env** — `NEO_TRACE_DIR` unset = no-op. The env is read
   ONCE per process and cached (tests use `_reset_cache()`;
   traceview's reader uses `_set_fallback_dir()` for post-hoc reads
   where the env isn't exported).
3. **ONE FILE PER TASK** — the harness's own `logs/{task_id}/trace.jsonl`
   stays THE authoritative full record; this stream is the cross-module
   OVERLAY (compact — no full prompts; depth lives where it lives).
4. **ts = time.time() epoch, 3 decimals** — matches harness trace.jsonl
   so merged views sort consistently.
5. **safe_segment** — one shared Win32-safe single-segment gate
   (separators, `:`-drives, null bytes, edge whitespace, trailing
   dot/space aliases). Implemented locally (shared imports nothing).

## Who emits what (landed)

| layer | events |
|---|---|
| runtime/scheduler | task_spawn, task_finish, run bookkeeping (both the run file and per-task streams) |
| runtime/worker | worker lifecycle markers (start, harness call boundaries, approval gate, finish) |
| runtime/model_router | `model_routed` per call (model, tier, hint, cost) — task_id from the router context |
| execution/sandbox | `sandbox_call` / `sandbox_result` per containerized command — task id derived from the mounted repo path (`logs/{task_id}/work`) |
| mcp_server | `memory_query` (per query; also a run-scoped journal) |
| harness | its own trace.jsonl remains the authority; traceview merges it in (no duplicate emission) |

## Bugs found while validating (fixed + test-pinned)

- **Win32 path rejection disabled ALL execution-layer tracing**:
  `_trace_task_id` in execution/sandbox.py rejected any raw path
  containing a backslash (a traversal defense) — on a Windows host
  every repo path is native-form, so sandbox events NEVER emitted.
  Fix: normalize separators for structure parsing, keep the `..`/`.`
  component rejection, single-segment safety via safe_segment on the
  extracted name. The old blanket rejection is now
  `test_task_id_from_work_dir`'s backslash-form assertions (native
  paths MUST trace).

## Files

| File | Role |
|---|---|
| `tracing.py` | emit/emit_run/readers; safe_segment; env caching + test hooks; `span`/`emit_span_start`/`emit_span_end`/`read_genai_spans`/`span_lifecycle`/`trace_id_for` GenAI span API |
| `traceview.py` | reconstruct_task + render_timeline + summarize + `reconstruct_spans`/`span_lifecycle` + the `python -m shared.traceview` CLI (`--spans`/`--lifecycle`/`--otlp`) |

## Tests

`tests/test_tracing.py` (38, Docker-less): record shape, no-op-off,
never-raise, reserved keys, run files, traversal-shaped ids (both
writer and readers), corrupt-line tolerance, scheduler dual-stream,
router context usage, the sandbox path adapter (incl. the Win32 fix),
cross-source chronological merge, summary counters, CLI modes.
`tests/test_ceiling15_spans.py` (9, Docker-less): all six span kinds
reconstruct a run, four-way correlation on every span, an unpaired start is
reported open rather than dropped, an exception becomes an `error` span and
is still re-raised, an unknown kind is normalized not dropped, a
legacy-only trace reconstructs as labelled derived spans, the CLI's
`--spans`/`--lifecycle`/`--otlp` modes, the never-raise guarantee, and
inertness when tracing is disabled.

## Deliberate scope

- The authoritative harness `trace.jsonl` remains owned by Terminal 1 and is
  never rewritten by shared readers, privacy views, retention, or exporters.
- The unified stream stays a compact JSONL overlay. It now passes through
  shared secret redaction, path/symlink containment, and an opt-in telemetry
  observation; it still does not duplicate full model bodies.
- OpenTelemetry export is an additive, read-only OTLP/JSON projection in
  `shared/otel.py`; it does not add a runtime dependency or alter the
  authoritative trace.
- Privacy, retention, injection review, and provider-health APIs are shared
  primitives. Cross-module producers/consumers must opt in explicitly; a
  security violation raises a typed failure rather than becoming a warning.

## Security, privacy, telemetry, and cost round (2026-09-25)

### Implemented shared surfaces

- `security.py` is the single bottom-layer trust-boundary policy. It provides
  recursive `redact_secrets`/`redact_text`/`contains_secret`, credential-safe
  key classification, `safe_relative_path`, `require_contained`/`safe_path`,
  symlink-component rejection, `scrub_environment`, source-labelled
  `detect_prompt_injection`/`adversary_review`, supply-chain manifest scanning,
  and the append-only `ApprovalAuditTrail`. `SecurityViolation` and
  `SecurityGateError` are typed failures; callers must not downgrade them to
  warnings.
- `threat_model.py` exposes a machine-readable model covering prompt injection,
  hostile repositories, MCP, plugins, skills, symlinks/traversal, secrets, and
  supply-chain packages. `validate_threat_model()` is a schema gate.
- `security_corpus.py` is the deterministic adversarial corpus used by the
  shared regression suite. Payloads are inert test data, never credentials.
- `privacy.py` provides `local_only`, `redacted`, and `shareable` derived views.
  All modes redact secrets; shareable mode additionally removes content,
  paths, and identities. `export_privacy_trace()` writes a separate file and
  never mutates its source.
- `telemetry.py` records event count, prompt/completion/total tokens, cost,
  context usage, latency, model/provider, and bounded attributes. Unified
  `tracing.emit()` calls `observe_event()` when telemetry is enabled, storing
  `_telemetry/<task>.jsonl` beside the overlay. `ProviderHealthTracker`
  persists non-secret success/failure/error-class/latency aggregates and
  exposes healthy/degraded/unhealthy status.
- `retention.py` applies age, byte, pattern, and keep-latest policies. It
  preflights the complete tree, refuses symlinks/outside-root paths, supports
  dry-run, and writes a redacted deletion receipt. `delete_task_data()` is the
  explicit task-directory deletion primitive.
- `otel.py` emits standard OTLP/JSON `resourceSpans` from derived records with
  deterministic trace/span IDs. `OTLPExporter` and `export_otel()` are
  read-only projections; the authoritative trace is never an output target.

### Tracing and viewer hardening

- `tracing.py` now redacts every emitted field, rejects credential-shaped
  task/run ids, refuses symlinked trace files/parents, and rejects symlinked
  IDs when listing or reading streams. It preserves the existing never-raise
  observability contract.
- `traceview.py` resolves every source through shared containment, redacts
  normalized records, and accepts `--privacy {local_only,redacted,shareable}`.
  A source symlink or outside-root path produces no reconstructed event.

### Verification and blocked lanes

- `tests/test_security_regressions.py`: **11 passed, 4 skipped**. The skips
  are Windows symlink-privilege/platform cases, not passes. The suite covers
  the threat model/corpus, redaction, tracing, containment, environment
  scrubbing, injection review, approvals, privacy, retention, telemetry,
  provider health, and OTel non-mutation.
- The exact Prompt 11 required command must still be run by the coordinator.
  Its pre-existing baseline in this dirty tree has two runtime-owned
  `runtime.model_router._record_usage` signature failures and one Docker skip;
  this module did not edit that cross-module file. Those failures remain an
  explicit handoff, not a shared-layer pass.
- No live-provider lane was run. No credential was read, printed, or retained.
  Docker-backed tests that require the daemon are blocked when the daemon is
  unavailable; the host-only shared tests are the applicable evidence here.

### Cross-module handoffs

- **Terminal 1 / harness:** route authoritative `TraceLogger`, prompts, model
  responses, status/report serialization, and state writes through
  `shared.security.redact_secrets`; call `adversary_review` on issue,
  instruction, skill, MCP, and web content before model/tool use. Preserve
  the existing trace schema and use `shared.telemetry.record_event` for
  context/cost observations.
- **Terminal 2 / execution:** use `require_contained`/`safe_path` for workspace
  and sandbox artifact paths, `scrub_environment` for every child process,
  and `append_approval_audit` for approval decisions. The execution workspace
  already has a local `scrub_env`; it should delegate or parity-check against
  this shared policy rather than fork it.
- **Terminal 3 / runtime:** call `record_provider_success`/
  `record_provider_failure` (or emit provider/outcome fields consumed by
  `observe_event`) and preserve the existing model-ledger contract. Provider
  health must never include raw exception text or credentials.
- **Terminal 4 / CLI/MCP/memory:** use `redact_secrets` for status, reports,
  session/state readers, and connector output; use `privacy_view` for
  shareable views and `retention.apply_retention` for operator-requested
  deletion. Do not expose raw trace data through status or MCP responses.

---

## VEX-PF-07 - Windows and no-colour parity for the daily path (2026-09-29)

**Files this round owned and created:** NEW `shared/platform.py`; NEW
`tests/test_daily_platform_parity.py` (131 tests). **Every file under `cli/`
was NOT edited** - including `cli/design.py` and `cli/tui.py`, which were
being rewritten by other terminals *during* this round (`cli/design.py`'s
mtime moved three times and it grew from 1237 to 3361+ lines, gaining the
`runline` and `announce` regions). **`harness/`, `runtime/`, `memory/`,
`execution/`, `evals/`, and `INTERFACES.md` were NOT edited.** **No
`harness/config.py` `DEFAULTS` key was added and none is needed** - see ?6.
No contract, event kind, journal field, exit code, or verifier mint changed.

Machine-readable handoff: `logs/product-round/terminal-07.json`.

### 0. Read this first - the four hazards are silent, not loud

A Windows user and a POSIX user run the SAME daily path, and the differences
never announce themselves. They are worth naming once, because every function
in `shared/platform.py` exists to make exactly one of them say something:

| hazard | the silent failure | the primitive |
|---|---|---|
| reserved device name | `write` reports success, creates no file, reads back empty | `is_reserved_device_name` |
| newline style | an LF or mixed file is rewritten as CRLF in full | `read_preserving` / `write_preserving` |
| path identity | `str(Path("C:/A")) != str(Path("c:/a"))` for one file | `path_equivalent` / `path_is_under` |
| capability | long paths and case-folding are per-MACHINE, not per-source-file | `capability_report` |

**Measure, do not assume.** `capability_report()` probes all three
filesystem capabilities on a real temporary tree and returns what it
happened. `available is True` means every probe RAN; a probe that could not
run leaves its own `*_reason` non-empty. The distinction is load-bearing:
collapsing "the probe failed" into "the probe found a hazard" would report a
machine that correctly measured the problem as a machine that could not
measure, which is the opposite of the truth.

### 1. `shared/platform.py` - the ONE authority, and what it replaced

Every public function is **total**: it never raises for hostile input, and a
refusal is a **returned sentence**, not an exception. A refusal a caller
cannot render is a refusal that gets swallowed.

* **`WINDOWS_RESERVED_NAMES`** - 22 names, `frozenset`. `COM0` and `LPT0` are
  deliberately ABSENT: a validator that over-refuses teaches people to work
  around the validator, which is worse than the bug it was added to fix.
  `is_reserved_device_name` judges the STEM (so `nul.txt` and `NUL.` are both
  caught, since Windows discards trailing dots) and case-folds.
  `first_reserved_segment` checks every COMPONENT, because a reserved name in
  a directory position is as unroutable as one in a filename position.
  `reserved_name_refusal` names the CONSEQUENCE, not the rule:
  *"NUL is a reserved Windows device name, not a file: writing it can report
  success, create no file, and read back empty. Rename it."*

  **This set was duplicated in three modules at three sizes**: 22 in
  `runtime/paths.py:35` and `cli/session.py:243`, and **4** in
  `memory/checkpoints.py:85` - which misses all 18 COM/LPT members. Import
  this one. The drift is deliberately NOT asserted as equal by this round's
  tests: doing so would be a permanently red test for a defect owned by three
  other files.

* **`NewlineStyle` / `detect_newline` / `read_preserving` / `write_preserving`**
  - `detect_newline` takes **BYTES**, because that is the only level at which
  the question is answerable: `

` and `
` are indistinguishable after
  universal-newline translation. A lone `
` is counted separately rather than
  folded in, so a file with one is not reported clean. The pair
  `read_preserving` + `write_preserving` round-trips a **mixed** file
  byte-for-byte, which `read_text`/`write_text` cannot do.
  `newline_report` is the receipt a caller branches on, and
  `needs_preserving_write` is `True` for exactly one case: `mixed`.

* **`normalize_relative` / `path_equivalent` / `is_absolute_anywhere` /
  `path_is_under`** - the ordering inside `normalize_relative` is load-bearing
  and was found by measuring, not by reading: `os.path.normcase` FIRST, then
  the separator swap, because on Windows `normcase` rewrites `/` back to `\`
  and doing the swap first produces a string whose prefix test silently never
  matches. `is_absolute_anywhere` asks BOTH grammars, because
  `Path("/etc/hosts").is_absolute()` is `False` on Windows and
  `PurePosixPath("C:/x").is_absolute()` is `False` - so a containment check
  written against the host's own `Path` mis-classifies the other platform's
  absolute paths, and a repository-relative check then treats `/etc/hosts` as
  relative. `path_is_under` case-folds, refuses a drive mismatch, and refuses
  `C:/repository` under `C:/repo`.

* **Probes clean up after themselves, scoped or not.** A probe that left a file
  in a caller's directory would be reported by every snapshot diff as a
  spurious change - and a snapshot diff is what decides whether a run is
  reviewable.

### 2. The required CRLF claim PASSES. Two neighbours do not.

Measured through the real `WorkspaceJournal` edit primitive, on real files:

| file shape | after one token edit | verdict |
|---|---|---|
| CRLF | `b'a\r\nN\r\nb\r\n'` | **preserved** - the required claim |
| LF | `b'a\r\nN\r\nb\r\n'` | **rewritten in full** (PF07-2) |
| mixed | `b'a\r\nN\r\nb\r\n'` | **homogenised** (PF07-2) |

So CRLF preservation on edit is real, and the two shapes nobody writes a test
for are broken. PF07-2's consequence is not cosmetic: with
`core.autocrlf=false` - what CI and every Linux checkout use - a one-token
edit to a **two-line** LF file makes `git diff --numstat` report **2
additions and 2 deletions**. The review surface, which is the product's
central promise, shows a whole-file rewrite. The remedy is already written
and tested; it is two one-line changes in files this round does not own.

### 3. Four measured defects, none fixed here, all reproducible

Full reproduction, owner, and remedy in `logs/product-round/terminal-07.json`.
Two are pinned as **named characterisation tests that PASS today and fail the
day the defect is fixed**, which is how a fix is made loud instead of silent:

* **PF07-1 (high)** `WorkspaceJournal.write("NUL", ...)` RETURNS `"NUL"` and
  the immediate `read` raises `FileNotFoundError`. Only `NUL` loses the write
  on this host - 21 of the 22 create real files - so a validator that only
  worries about `NUL` is under-inclusive on a host that resolves a different
  one. The refusal covers all 22 anyway.
* **PF07-2 (high)** as ?2. `WorkspaceJournal.read/edit` and
  `harness/agent_loop.py:3364/3372`.
* **PF07-3 (medium)** `cli/tui.py:420` - `_ROLE_RE`'s class
  `[a-z0-9.]` **excludes the hyphen**, so `_m()` never matches a hyphenated
  role token, never applies its `none` fallback, and Textual then raises
  `MarkupError: auto closing tag ('[/]') has nothing to close`. Measured:
  `_m('[neo.some-role]text[/]')` returns it verbatim;
  `Content.from_markup('[/]')` raises. **19 `_m` call sites; ONE
  (`NeoApp.transcript`) has a try/except that falls back to escaped text, so
  no message is deleted there - asserted, and it holds. The other 18 have no
  fallback.** Fix is one character in the class.
* **PF07-4 (low)** `cli/theme.py:770` gates `hue_collisions` on `hue_on` but
  computes `hue_collision_count` unconditionally, so under `NO_COLOR` the
  report is `{'hue': False, 'hue_collisions': [], 'hue_collision_count': 3}`.

### 4. Layout: measured at every viewport, and the gate can actually fail

`out_of_bounds() == ()` at **15 combinations** (60/80/100/120/200 x
auto/show/hide, `statusline_rows=1`), plus split `60x24` and vertical
`50x160` - both of which collapse the rails to 0 columns rather than
squeezing them. Zero of 35 rendered surfaces exceed their viewport.

**"No layout jump" needed a better definition than "the width changed."** The
transcript is a proportional region, so its width changes on *every* column
and a change-detection test is vacuous. The meaningful property is the
reflow **slope**, and it changes at exactly `auto -> [120, 121, 122, 160,
161]` and `show -> [96, 97, 120, 121, 160, 161]` - every one a declared
constant (`SIDEBAR_BREAKPOINT=120`, `PLAN_MIN_COLUMNS=96`,
`ULTRAWIDE_COLUMNS=160`) or the settle column beside one. The sidebar appears
at exactly 121 and is hidden at exactly 120; both rails are monotone over
40..240, so neither ever appears and disappears.

**The overlap gate was mutation-tested**, because a gate that cannot fail is
worse than no gate. Three mutations were applied to `cli/design.py`, each run,
each **restored byte-identically** (verified by full-text comparison, not by
git): widening the transcript 4 columns into the sidebar -> FIRED; making the
run line 3 rows -> FIRED; moving the run line into the composer -> FIRED. A
fourth mutation correctly did NOT fire: side-by-side rails cannot collide by
widening the outer one, they just go off-screen, which `out_of_bounds()`
catches instead.

The band rule is **derived from geometry, not from a name list**: a region is
a band carved from a host when it is strictly SHORTER and its rows fall
inside the host's rows. Width is deliberately not part of it - the run line
and announcement span the FULL terminal width and sit below both the
transcript and the context rail, because the app is one
`Screen { layout: vertical }`. Deriving it this way is what let the suite
survive `cli/design.py` gaining two new regions mid-round without being
edited.

### 5. A layout convention that is a trap, recorded rather than called a bug

`Region.height` for `transcript` is the content **AREA**, not the scrollable
height. `resolve_layout(60, 36)` reports `transcript y=1 h=31` with the run
line on row 30 and the announcement on 31, so the bands are carved from the
bottom of the area and the scrollable height is `31 - 2 = 29`. The app's own
CSS agrees with that subtraction, so the two are **consistent** - but a
consumer that reads `Region.height` as the scroll viewport over-reports by
the band count. Documented on `Region` or published as a `scrollable_rows`
field would close it. `cli/design.py` is another terminal's file and was not
edited.

### 6. No `DEFAULTS` key, and the anti-clutter rule obeyed

Nothing behaviour-changing went into `harness/config.py::DEFAULTS`. A value
there merges into every `Task` and every eval arm, and "this host supports
long paths" is not a fact about a run. Every function added is a pure
predicate, a refusal sentence, or a measured receipt - there is no opt-in
behaviour to gate, so there is no key.

Anti-clutter: `design.ANTI_CLUTTER_MIN_ENTRIES == 3`, and
`cli.toggles.MIN_SECTION_ENTRIES`, `cli/tui_components.MIN_SECTION_ENTRIES`
and `cli/review.MIN_REVIEW_ROWS` are asserted **equal** to it - so the rule is
one number in four places. 0/1/2 entries publish nothing at all (no heading,
no indicator); 3+ render. An idle shell's statusline region is `(0,0,0,0)` and
costs zero rows.

### 7. A measurement artefact worth recording, because it nearly became a bug report

An early probe reported **752 ESC bytes** from `capabilities --json`. It was
wrong, and the error was in the harness: PowerShell's `Out-String` injects
its own console formatting. Measured through Python's `subprocess` with a real
pipe the count is **0**, and seven real child processes (isolated
`HARNESS_HOME`, every provider key stripped from the child env) all carry
zero ESC, zero C0 and zero bells. The number is recorded here rather than
quietly dropped, because "the pipe was dirty" is exactly the kind of claim
that gets filed and then has to be retracted.

### 8. What the next terminal must know without re-reading the code

1. **`shared/platform.py` is the one reserved-name set.** Three other modules
   restate it; `memory/checkpoints.py` has the wrong size (4, not 22).
2. **Do not use `read_text`/`write_text` for a read-modify-write of a user's
   file.** Use `read_preserving`/`write_preserving`. This is PF07-2 and it is
   the highest-value one-line change available in the tree.
3. **Do not compare paths with `str()`.** Use `path_equivalent` /
   `path_is_under`. `relative_to` is case-sensitive even where the filesystem
   is not, so a case change defeats a containment check written on it.
4. **`Region.height` on the transcript is the area, not the scroll viewport**
   (?5).
5. **`tests/test_daily_platform_parity.py` has two tests that PASS on a known
   defect.** They are named as characterisation tests and their docstrings say
   so. Do not "fix" them by asserting correct behaviour - retarget them when
   the defect is fixed.
6. **Every test in that file deletes `NEO_EFFORT`** via an autouse fixture,
   because `cli.commands.apply_effort` leaks it and poisons
   `tests/test_config_trace_state.py::test_get_config_handles_none`. That leak
   is `cli/commands.py`'s and is still unfixed.
7. **`pytest-randomly` is NOT installed on this host**, so `--randomly-seed`
   is unavailable and the `-p no:randomly` in the required commands is a
   no-op. Order sensitivity was measured by file POSITION instead.

### 9. Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)

- **`tests/test_daily_platform_parity.py` -> 131 passed** (42.05 s). Real temp
  trees under the runner's own `tmp_path`; **no filesystem mock anywhere in
  the file**. 7 real child processes. No Docker, no provider, no network.
- **`tests/test_cli_theme.py` -> 38 passed**, twice, identical to the
  pre-round baseline.
- `test_design_layout` + `test_cli_terminal_ux` + `test_cli_terminal_parity`
  -> **151 passed**; `test_diff_review` + `test_model_picker` + `test_auth_flow`
  -> **305 passed, 1 skipped**; `test_ceiling_r2_15_trust` +
  `test_ceiling_security` + `test_workspace_security` + `test_security_regressions`
  -> **168 passed, 4 skipped**. **The 5 skips are pre-existing Windows
  POSIX-chmod and symlink-privilege cases and are NOT counted as passes.**
- Wider lane (`design_layout` + `terminal_ux` + `terminal_parity` +
  `diff_review` + `model_picker`) -> **346 passed** in 242.14 s.
- Order/contamination: my file **first** (with `test_config_trace_state`) ->
  246 passed; **last** -> 214 passed; **middle** (with `test_cli_theme`) ->
  238 passed; all five classes selected separately in one process -> 90 passed.
- `python -m ruff check --no-cache shared/platform.py
  tests/test_daily_platform_parity.py` -> **All checks passed**.
  `python -m compileall -q` clean on both.

**One red happened during the round, was not mine, and is now green.**
`tests/test_cli_theme.py::test_the_token_module_is_the_only_palette` went red
mid-round on `design.py:_DEFAULT_COLORS` and two siblings. That test globs
`cli/*.py` at line 734; this round created no file under `cli/` and edited
none. The cause was a contrast-audit helper in `cli/design.py` that read
`cli.theme`'s private palette tables **by name** to audit every palette
including the 16-colour fallbacks. `cli/design.py` no longer contains those
markers (0 occurrences) and the gate is green. **The finding worth keeping:
the gate matches a bare substring and cannot tell a palette DEFINITION from a
REFERENCE to one**, so a legitimate auditor trips it - the "a gate that
punishes correct behaviour" class this tree's own notes warn about. If it goes
red again for this reason, scope the match to a definition; do not delete the
marker from the auditor.

### 10. Not run / not claimed

- No Docker lane, no live-provider lane, no credential inspected, printed or
  retained.
- No native Windows **ConPTY** capture, and no attached-PTY campaign and no
  screenshot. The Windows evidence is real filesystem I/O through real
  temporary trees and real piped child processes - which is what the brief
  asked for - but it is **not** an attached pseudoconsole, and
  `logs/terminal-ux/terminal09_conpty_check.py` remains unproven on this host.
- No **POSIX** lane. Every number here is from `win32`. The suite is written
  to be platform-honest (the two filesystem claims gate on the measured
  capability report, not on `os.name`) but it has only ever been RUN on
  Windows, and that is stated rather than implied.
- `python -m evals.run` was **not** run: this round changed no prompt.
- No full-suite run. Every lane above was run and every failure attributed.
- **Shared dirty tree.** Nothing was reset, cleaned, checked out, restored,
  stashed, rebased, staged, committed, pushed, tagged, or uploaded. The only
  files this round created are `shared/platform.py` and
  `tests/test_daily_platform_parity.py`; the AGENTS.md section is an append.
  `cli/design.py` was mutated three times to prove the overlap gate can fail
  and restored byte-identically each time.
---

## T5 P0/W1 - hostile-character normalisation in the redactor (2026-10-01)

**Files:** `shared/security.py` (added `normalize_for_redaction`,
`NormalizationReceipt`, `redact_text_report`, `_withheld_text`,
`_withheld_scan`; made `redact_text` / `redact_text_scanned` fail closed),
`shared/security_corpus.py` (obfuscation cases + homoglyph/control fixes),
NEW `tests/test_redaction_hardening.py` (78 tests).

**The defect.** Every secret rule matches on a strict ASCII character class, so
one inserted invisible character defeats it. `ghp_abc<ZWSP>def...` matches no
rule: `github_token`'s body class is `[A-Za-z0-9_]{12,}`, and the split token has
a gap in it. The redactor's job is to be the LAST thing a payload passes, and
this was a one-character bypass of it.

**The fix, and why it is normalisation rather than wider classes.** Loosening
each rule's class would mean re-auditing every rule for what the wider class now
matches - and a rule whose class spans invisibles is a rule that also spans
whatever else someone puts there. Normalisation removes the four classes before
any rule runs, so the rule table and its character classes are unchanged, and
the existing equivalence tests over the rule set still hold.

Four classes, four mechanisms:

| class | mechanism | what it defeats |
|---|---|---|
| ANSI CSI / OSC | `_ANSI_OSC_OR_CSI`, `_ANSI_SINGLE` | escape-split bodies; OSC strips its title text too |
| invisible | U+200B-200F, U+202A-202E, U+2066-2069, U+FEFF | zero-width / bidi / BOM splits |
| other controls | `_OTHER_CONTROLS` | NUL, BEL - dropped by terminals but not by a class |
| homoglyph | `_CONFUSABLES` | a Cyrillic letter inside a KEY NAME |

**Five decisions a later terminal must not undo.**

1. **The invisible set is built from CODEPOINT RANGES, not literal characters.**
   A ZWSP inside a source constant renders as nothing at all in most editors,
   so a table written with literal invisibles is unreviewable: you cannot see
   what is in it. Ruff's `RUF003` caught the same class of hazard in the
   COMMENTS afterwards - the prose claimed to explain the danger and then
   committed it. Both are now codepoint lists.
2. **`_CONFUSABLE_PAIRS` covers the full lower-case Cyrillic set**, because
   `api_key` and `secret` are spelled entirely from it. Measured: the first
   table had `a e o p c y x i s j h d` and no `k`, so `api_key` with a Cyrillic
   KA folded to `api_ky` - a string no rule owns. A partial table is worse than
   none, because it reads as coverage. `test_every_cyrillic_lowercase_lookalike_is_folded`
   guards the whole a-z range, and
   `test_the_homoglyph_table_is_not_a_universal_translation_table` guards the
   other direction: a character with no security-relevant look-alike must pass
   through untouched, or the table starts rewriting legitimate non-ASCII prose.
3. **A clean string returns the SAME OBJECT.** `test_a_clean_input_returns_the_same_object`
   asserts identity, not equality. This redactor is on every journal write, every
   trace row and every privacy export, so the common case must cost one cheap
   pre-test per class and no allocation.
4. **`normalize_for_redaction` coerces through `_as_text`,** so `None` returns
   `""` and a hostile `__str__` RAISES into the caller's fail-closed handler.
   Returning the input unchanged would hand a non-`str` back to a function that
   promised a `str` - the coercion contract of `redact_text` and this function
   would disagree.
5. **The receipt's counts are real counts, never defaulted zeros.** This is the
   whole point of the type: a receipt that says "0 controls removed" when the
   pass never ran is the dishonesty this repo exists to prevent, so
   `_withheld_scan()` names `redactor_unavailable` in `skipped_rules` rather
   than reporting a clean scan. `redact_text_scanned` failing closed returns
   `(marker, withheld scan)` - never `(marker, zero scan)`.

**Measured on this host** (`Python 3.10.11, win32, AMD64`, best of 5, whole
`redact_text` including the rules, not just the pre-scan):

| input | time | 4x scaling |
|---|---:|---|
| `"y" * 40_000` | 0.0094 s | - |
| `"y" * 400_000` | 0.0933 s | 4.20x (match) |
| `"a" * 400_000` | 0.0903 s | 4.33x (**non-match**) |
| 50k ANSI fragments | 0.0049 s | 4.18x |

Budget is 1 s; worst measured is **0.0933 s**. The non-match case is the one
that matters for linearity: it is the input where every rule walks the whole
text and matches nothing, and it scales at 4.33x for 4x input. Before the change
the same three shapes measured 0.023 / 0.242 / 0.232 s.

**Two measurement mistakes worth recording, because both first produced a FALSE
PASS and are the reason the corpus looks the way it does.**

* *Longest contiguous run of the secret in the raw output.* This UNDERCOUNTS.
  `AKIAIOSFOD<ZWSP>NN7EXAMPLE1` has two 10-character runs, so it clears any
  threshold - while a reader, who cannot see the ZWSP, recovers the whole token.
* *Longest common subsequence over the output.* This OVERCOUNTS, badly: the
  canary scored 14 against a **fully redacted** line purely from the
  surrounding English prose.

The metric that survives both is **contiguous run length measured against the
PERCEIVED output** (invisibles stripped). `_visible_run` in the suite does that,
and it is the reason `test_no_obfuscated_canary_survives_into_the_output`
asserts on what a reader recovers rather than on a substring of the bytes.

**The corpus: 5 of 7 obfuscated cases are load-bearing, and that number is
measured, not asserted.** Load-bearing means: with normalisation disabled, the
raw output still shows a readable run of the secret; with it enabled, it does
not. The other two are defended raw as well and are kept anyway, to prove the
rules do not misfire. A corpus whose failures cannot be interpreted is a corpus
nobody can maintain, so each case names its mechanism in its `case_id`
(`-strict-body`, `homoglyph-key`) and the non-vacuity gate
(`test_at_least_five_cases_are_load_bearing_for_normalisation`) fails if the
load-bearing set ever empties out - which is the way a corpus stops testing
something while every end-to-end assertion still passes.

**A case that "passed for the wrong reason", fixed by measurement.** The first
version of the obfuscated cases appended the hostile character AFTER the token
(`secret + ESC`). The token body stayed contiguous, so `github_token` still
matched the raw form and the case proved nothing - while passing. The insertion
has to go INSIDE the body, and `_split_token` now takes an index for exactly
that reason, with the two wrong versions recorded in its docstring.

**Security-corpus additions:** 7 `obfuscation` cases
(`ansi-split-credential`, `zero-width-split-credential`, `bidi-override-token`,
`homoglyph-key-name`, `quoted-secret-after-ansi`, plus 5 `strict-body-*`
variants including NUL and BEL control splits), all measured redacted.
`OBFUSCATED_CANARY_EXFILTRATION_CORPUS` + `iter_obfuscated_canary_cases()`
carry 7 canary cases across 6 untrusted sources. **All 7 leak 0 secrets into the
output**, measured with the perceived-output metric.

**Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)**

- `tests/test_redaction_hardening.py` -> **78 passed in 1.86 s**.
- `tests/test_security_regressions.py tests/test_ceiling_security.py
  tests/test_workspace_security.py tests/test_redaction_hardening.py
  tests/test_eval_status_vocabulary.py` -> **224 passed, 4 skipped**. The 4
  skips are pre-existing Windows symlink-privilege cases and are **not** counted
  as passes.
- `python -m ruff check` clean on `shared/security.py`,
  `shared/security_corpus.py`, `tests/test_redaction_hardening.py`,
  `tests/test_eval_status_vocabulary.py`.
- The linearity table above, best of 5, on the shipped code.

**Known limits, stated rather than left silent.**

* **Normalisation cannot help a rule it cannot reach.** It strips invisibles and
  folds look-alikes; it does not decode percent-encoding, base64, or a ROT-N.
  A payload wrapped in base64 is invisible to every rule here and to this pass.
* **The receipt reports `changed`, not a redaction-outcome diff.**
  `NormalizationReceipt.raw_would_have_leaked` currently mirrors `changed`, so
  it answers "was the text altered", NOT "would the raw text have leaked". Those
  are different questions and the field name implies the second one. Renaming it
  to `changed` / `caller_should_strip` would be honest today; computing the
  actual raw-vs-normalised redaction outcome would be better and is not done.
* **The homoglyph table is deliberately narrow** and therefore WILL miss a
  look-alike outside it (Coptic, Cherokee, fullwidth Latin). Widening it is a
  policy call about rewriting user text, not a security fix.

**Not mine, proved not mine.** `tests/test_ceiling16_surfaces.py::TestDocsTruth
Gate::test_the_real_tree_passes` and `::test_cli_entry_point_exit_codes` fail on
a docs version drift (`site/src/lib/content/releases.ts` declares 0.2.1,
`pyproject.toml` declares 0.3.0). This round edited neither file.
