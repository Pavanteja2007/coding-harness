# Phase 2 handoff — Trusted Execution

**For all five terminals.** Phase 2 is the **subtractive** phase: 3 engines → 1,
delete the legacy loop, remove the unsandboxed bash path, plus the permission
model and the edit cascade.

**Read `phases/DOCTRINE.md` §1 and §5 first.** Phase 2 is where the repo's
worst promises are either kept or broken, and §5's rule — *never execute content
you have not reviewed* — currently names two unscanned sources (**issue text**
and **repository instruction files**). That is the phase's actual headline, and
it is a deletion problem like the rest of it.

**Starting position:** G1 is **`G1_RED`** (`docs/release-verdict.md`). Rung #9's
cold retrieval is over budget with no cause claimed; rung #10 has four
T4-owned reader-facing defects; the full suite has 23 adjudicated failures.
**Nothing in this brief assumes Phase 1 was green.**

---

## 1. The pinned unsandboxed-bash defect, and why the pin going green is the proof

This is the registry's **only** `KNOWN_FAILING_PINS` entry, and it is the first
thing Phase 2 should close.

```
node    execution/test_unsandboxed_pins.py::
        test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub
owner   T1 / P2.1
status  KNOWN FAILING — the pin passes today BECAUSE the defect is present
```

**The defect.** `harness/agent_loop.py` reaches `from harness._stubs import
sandbox` and calls the local-subprocess `execute_sandboxed` **unconditionally**
for the `legacy_agent` engine. `harness/agent_loop.py:787-795` is the location
T1's own handoff recorded; the pin re-derives it by AST, so the location moves
but the pin does not.

**Why the pin going green IS the proof, and not just a side effect.** The pin is
an *inverted* pin: it asserts the defect is still there. The registry's own
vocabulary makes the transition explicit — when the gap closes, the pin's
observation becomes `PASSED` and `classify_known_failing` returns:

```
promote_me
PROMOTE THIS. The recorded gap is CLOSED: the pin passes. Owner was T1 / P2.1;
it closes when harness/agent_loop.py deletes the `from harness._stubs import
sandbox` reach and the local-subprocess execute_sandboxed call that follows it.
Nothing else in the repository changes.. Delete the entry from
KNOWN_FAILING_PINS in the same change that closed the gap, so the closure is
visible in the diff.
```

So the proof is a **two-part** event and both parts are required:

1. `python -m tests.known_failing_pins` exits **non-zero** with `PROMOTE THIS`,
   in **every** lane (the registry gate is the first step of all six);
2. `KNOWN_FAILING_PINS` loses its only entry **in the same commit**.

A commit that only does (1) leaves the tree red. A commit that only does (2)
deletes a recorded gap that is still open — which is the one thing the registry
exists to prevent.

**It fails LOUD when Docker is down.** Do not "fix" this by falling back to the
host. `execution.sandbox.SandboxUnavailableError` must propagate; a
`local_trusted` fallback is a *second* boundary decision and it is not one.

---

## 2. The three-engine collapse

Phase 1 left three engines reachable. Phase 2 leaves one.

| engine | where | status going into P2 |
|---|---|---|
| `daily` (`DailyCodingStrategy`) | `harness/agent_kernel/strategy.py` | **the default**; the one path with a conversation, a turn ledger, checkpoints, compaction and knowledge |
| `verified_fix` (`VerifiedFixStrategy`) | `harness/core.py` | reachable via `neo fix`; owns the verifier gate and the `state.json` six-key prefix |
| `legacy_agent` (`_run_agent_legacy`) | `harness/agent_loop.py`, ~1 255 lines | **compatibility surface**, fully supported, and the home of the unsandboxed bash path above |

**Which becomes the default: `daily`.** It is already the resolver's answer
(`harness/agent_kernel/kernel.py::resolve_agent_strategy` returns
`("daily", "default")`), it is the only engine with the conversation and
checkpoint machinery, and rung #8's guarantee was *measured on it*.

**What gets deleted, in this order — do not reorder it:**

1. **`harness/agent_loop.py::_run_agent_legacy`'s own EDIT/WRITE/BASH verbs.**
   They bypass the typed catalog, so they carry none of the digest, uniqueness,
   protected-path, approval or undo guards. The replacement already exists
   (`harness/tools.py::apply_catalog_edit` /
   `execution.workspace.SafeToolBackend.execute`); AGT-03 filed the exact patch.
2. **`harness/agent_loop_step.py`'s four hard-coded bounds.** AGT-11's purity
   pins are `step()`'s, and Phase 1's wave deleted four `25` literals for
   exactly this reason — do the same for whatever is left.
3. **The unsandboxed bash path** (§1). Only reachable from `legacy_agent`, so
   it dies with (1).
4. **`harness/core.py::_run_task_legacy` / `_run_agent_legacy`** — the private
   compatibility adapters. Already private; this is the deletion.

### The parity matrix that must pass FIRST

`tests/test_agent_loop_matrix.py` is the instrument, and it already runs all 14
scenarios **twice** — against the pure `step()` with a scripted boundary, and
through the real `run_agent_stepped` on a real repository — asserting the two
statuses **agree**. That agreement is the proof the adapter contains no
decision.

**The parity matrix must pass before any deletion, and again after each one:**

| # | scenario | what it pins |
|---|---|---|
| 1 | question | read-only end state |
| 2 | research | the read-only tool set |
| 3 | build | multi-file |
| 4 | fix | the verifier gate |
| 5 | multi-file | atomic edit groups |
| 6 | tool failure | reflection budget |
| 7 | policy refusal | never charged |
| 8 | context exhaustion | compaction |
| 9 | resume | checkpoint identity |
| 10 | cancel | **2 skips — a real in-flight interrupt; BLOCKED coverage, not passes** |
| 11 | doom loop | `needs_input`, not a silent refusal |
| 12 | unparseable tool call | one retry nudge, never a crash |
| 13 | approver denial | fail closed |
| 14 | network unavailable | honest degradation |

**Two rows are already known-red and are NOT this phase's to fix silently:**

* `tests/test_agent_loop_matrix.py::TestTheMatrix::test_the_pure_step_reaches_
  the_expected_status[tool_failure]` is **order-dependent**: the case's tool
  table is built by a CALL (`tools={"bash": _clash()}` at `:568`) evaluated once
  at `MATRIX` construction, so `_clash()`'s `state` closure is **shared** by
  every test that parametrizes on `tool_failure`. Whichever runs first consumes
  the failure; the rest see `ok=True` and produce no `reflection` event. With
  `pytest-randomly` installed the lane is red in ~4 of 6 runs. **Fix: make the
  tool table a FACTORY** (`default_factory=dict` + `tools=lambda c: {...}`
  resolved in `_tools_for`, or one `_clash()` per test). This is T5's file and
  it is filed, not fixed.
* `harness/test_config.py::test_config_guard` **errors on collection** — a
  production module named `test_*.py` whose public function pytest collects
  with two fixtures that do not exist. Every `pytest harness/` run ends in that
  ERROR. Owner **T1 + T5**: rename the module to `config_guard.py` (7 call
  sites in `tests/test_ceiling_r2_03_config_guard.py`) or rename the function.

---

## 3. Permission-model changes that will affect `cli/` (T4)

**Nothing in `cli/` changes shape, and three things change what a surface can
say.**

1. **`harness/approver.py` still has NO production call site.** The API and the
   tests exist; nothing calls it. Today `cli/interactive.py::_trusted_approver`,
   `cli/tui.py`, `harness/agent_loop.py::_request_approval` and
   `runtime/approval.py` each ask a human themselves. Phase 2's mount shape is
   in `harness/AGENTS.md` §6. **A CLI approver (the human's own `y/n`) must be
   consulted for the SAME request the reviewer saw; if the two disagree the
   human wins, and the reviewer must never be allowed to satisfy the human's
   slot.**
2. **`shared/approval.py::resolve_daily_trust` is the one resolver** for the
   daily path's containment. `cli/` should read the `DailyTrust` receipt, never
   `agent_process_sandboxed` directly. On a conflict the resolver reports the
   **weaker** boundary, so a receipt can never overstate containment — which
   means a UI that reads the raw key can.
3. **`harness/codemod.py`'s two tools are in the ONE catalog but on NO role
   profile**, so every agent is default-deny on them. Exposing
   `rename_symbol` / `update_signature` to a role is an owner decision (T4 for
   `cli/`, T3 for `runtime/roles.py`), and `cli/commands.py`'s `plan` mode still
   carries its own hand-written `visible_tools` that duplicates
   `runtime.roles`' — the same drift `harness/knowledge.py` removed.

**Four rung-#10 defects land on T4 and they are all reader-facing**, so they
belong in this phase's scope rather than being deferred:

* LINK 3: `cli/runview.py`'s projection re-derives every field instead of
  carrying the reconstructed one, so `module`/`source` never reach the card.
* A `retrieval_truncated` journal row reaches **no run fact and no card line**,
  while `cli/runview.py` lists it in `INFORMATIONAL_EVENTS` — consumed and never
  surfaced. `harness/knowledge.py::render_truncation_note` exists and has no
  production call site.
* The card has **no vocabulary for an absent value**. `cli.runview.briefing_lines`
  already has the convention (`if not data.get("available")` → an honest line);
  `card_lines` and `ResultCard` have none, so an absent field is simply omitted
  and reads as "there was nothing to report".
* An **unpriced** run renders `$0.000000` with no `unpriced` and no `vacuous`
  marker. `DOCTRINE.md` §1 forbids the first and §3 requires the second.

**Also T4, and unchanged since P0:** `cli/runview.py::EVENT_VOCABULARY` is
missing a long tail of kinds (`reflection`, the four steering kinds,
`verification_rung`, `turn_cap_approaching`, `policy_refused`,
`environment_unavailable`, `compaction_thrash`). Six terminals have filed the
same one-line fix. It is one line and it is the reason a live TUI run renders
new rows as unmapped.

---

## 4. The vendoring Phase 2 will do, and the header each must carry

### 4.1 OpenCode's edit cascade — **MIT**

Phase 2 vendors the *edit cascade* (multi-file symbol rename propagating through
definition, call sites and importers).

**The header, verbatim — `scripts/provenance_report.py` parses exactly these
keys:**

```
# vendored: yes
# upstream: https://github.com/sst/opencode
# upstream-commit: <40-hex-sha>
# upstream-path: <path/to/the/file>
# licence: MIT
# beats-ours-because: <the specific property that made copying better than writing>
# modified: yes
```

**`beats-ours-because` is MANDATORY and is not a formality.** `provenance_report.py`
treats a missing or empty one as an **incomplete declaration**, which fails under
`--strict`. The honest one-liner for this vendoring is the *recall over
precision* property: our `harness/codemod.py` resolves Python precisely by AST
role classification and **refuses** on anything it cannot prove; OpenCode's
cascade is deliberately over-approximate and reports what it could not resolve as
advisory rather than blocking. If Phase 2 wants both, the vendored code is the
advisory arm — **say that, not "it's faster"**.

### 4.2 Aider's RepoMap — **Apache-2.0**

`harness/repomap_cache.py` already carries the header and
`NOTICE` is current. **If Phase 2 vendors more of it, two obligations:**

* the `NOTICE` file must carry the Apache-2.0 attribution — that is the whole
   reason Apache-2.0 was chosen over MIT for this one (one licence determination
  across all vendored artefacts is cheaper to maintain than two);
* **Apache-2.0 has a patent grant and a NOTICE obligation that MIT does not.**
  A file that copies Aider code and declares `licence: MIT` is a licence error,
  not a preference.

### 4.3 The **binary** provenance shape is different, and Phase 2 will hit it

`provenance/binaries.json` exists because **a binary has no header**. A binary is
pinned by a **VERSION** and a **SHA-256 OF THE FILE**, not by a commit, and the
digest is re-hashed from disk on every run — a mismatch is `mismatched`, kept
separate from `incomplete` because that is the supply-chain case.

**The open gap Phase 2 inherits, measured:** ripgrep 14.1.1 and `fd` 10.2.0 are
fetched at run time with their **versions pinned** in `harness/search_engine.py`,
but their **digests are read from an upstream `.sha256` sidecar at download
time and are not committed anywhere in this repository.** So the expected digest
travels *with the payload*, and **a replaced asset with a replaced sidecar is
accepted.** A checksum-verified download whose checksum is not pinned is the
paperwork version of the same honesty problem. Remediation is one file: commit
the eight digests under a `committed_digests` section. Owner **T1 / P2.1 +
T5**.

Two licence facts, corrected against the brief that introduced this work:
**ripgrep is `MIT OR Unlicense`, not "MIT"** — MIT is relied on because it
carries an attribution-and-notice obligation `NOTICE` can discharge and Unlicense
would not; **fd is `MIT OR Apache-2.0`**, MIT relied on for the same reason.

### 4.4 One number, several readers — the check to add

Found while measuring Phase 1's cost (see `docs/release-verdict.md` §2):
`harness/config.py` says the turn ceiling is **60**,
`harness/turn_caps.py` resolves **60**, and `runtime/turn_cost.py::
TURN_CEILING_NEW` says **50** — so the module that *prices* the ceiling has the
wrong ceiling in it. Any vendored component that carries a **limit** (a batch
size, a ceiling, a token budget) must read it through the authority rather than
restate it, and the provenance header should say which authority.

---

## 5. What Phase 2 must not do

* **Do not turn a `blocked` row into a `pass`.** The vocabulary is
  `pass | fail | blocked + reason | not_implemented + owning phase`, and `skip`
  is not a member. Two gates now publish it (`evals/gates/P0.py`,
  `evals/gates/P1.py`) and both **import** the vocabulary from P0 rather than
  redeclaring it, because two lists is how `skip` gets added back.
* **Do not put a host-dependent timing assertion in a blocking lane.** Two of
  the six lanes are non-blocking for exactly this, and the reason is written
  into the workflows: *a flaky timing test in a blocking lane trains people to
  ignore the gate.*
* **Do not fall back to the host when Docker is down.** Fail loud. The
  unsandboxed path is the thing this phase deletes.
* **Do not widen the verifier mint.** `harness/agent_loop_step.py` contains no
  string literal equal to `success` outside its docstrings and a source-level pin
  enforces it. There are still **four** mint sites where `DOCTRINE.md` §2 claims
  one, with **two** recorded divergences (`legacy.py` defaults a missing
  `regression_passed` to `True`; `verified.py` and `legacy.py` read an
  error-bearing evidence block as clean). Collapsing them is a *behaviour*
  decision with a Change Log entry — not a side effect of the engine collapse.
* **Do not edit another terminal's file in a shared worktree and write a stale
  snapshot back.** This round's rung-break demonstration did exactly that and a
  digest check is what caught it; the fix (a conditional restore that reports
  `restored` / `superseded` / `restore_refused`) is in
  `evals/rung_break_demos.py::GuardedEdit` and is worth copying.

---

## 6. Cross-terminal requests, precise enough to act on without asking

| # | owner | file | what | why |
|---|---|---|---|---|
| 1 | **T1** | `harness/agent_loop.py` (~787-795) | delete the `from harness._stubs import sandbox` reach and the local-subprocess `execute_sandboxed` call; resolve the boundary like the kernel does and **fail loud** when Docker is down | §1; the registry's only `KNOWN_FAILING_PINS` entry |
| 2 | **T1 + T5** | `harness/test_config.py` | rename the module to `config_guard.py` (or the function), and update 7 call sites in `tests/test_ceiling_r2_03_config_guard.py` | every `pytest harness/` run ends in a collection ERROR |
| 3 | **T1** | `harness/agent_loop.py` | route the legacy EDIT/WRITE verbs through `harness.editor.apply_text_edit` / `harness.tools.apply_catalog_edit`; the patch is in `harness/AGENTS.md` | §2 step 1; three defects disappear at once |
| 4 | **T1** | `harness/agent_kernel/kernel.py` | `SessionController.run_turn` still records `strategy_source="spec"` for an unqualified run; `agent_strategy_source` on the result carries the accurate one | a receipt should not name a source that did not supply it |
| 5 | **T2** | `execution/walk_scope.py` | import `shared.skipset` instead of declaring `SKIP_DIR_NAMES` (49 names — it prunes **more** than the authority) | 9 independent skip-set tables is `DOCTRINE.md` §8 with better branding |
| 6 | **T3** | `runtime/turn_cost.py` | `TURN_CEILING_NEW = 50` vs the shipping **60**; read it through `harness.turn_caps.resolve_caps` | the module that prices the ceiling has the wrong ceiling |
| 7 | **T3** | `runtime/difficulty.py`, `runtime/subagents.py`, `runtime/analyze_history.py` | same as (5) — three tables, 6/11/19 names | same |
| 8 | **T3** | `runtime/*` | `import litellm` costs **5.37 s** on this host; `evals/rung_break_demos.py` shows a module-scope `import litellm` in `cli/interactive.py` blowing the 2 000 ms startup budget to **9 336 ms** (median of 3) | startup is rung #9's fastest metric and it is one accidental import away from failing |
| 9 | **T4** | `cli/runview.py`, `cli/tui_components.py` | the four rung-#10 defects | §3 |
| 10 | **T4** | `cli/runview.py::EVENT_VOCABULARY` | one line; six terminals have filed it | a live TUI run renders unmapped kinds |
| 11 | **T4** | `cli/fileview.py`, `dashboard/collect.py` | import `shared.skipset` (18 and 9 names) | 9 tables |
| 12 | **T5** | `tests/test_agent_loop_matrix.py` | make the `tool_failure` tool table a **factory** (`_clash()`'s closure is shared across four tests) | the lane is red in ~4 of 6 randomly-seeded runs |
| 13 | **T5** | `tests/test_scheduler_integration.py:354` | the test expects `[REDACTED]`; `shared/security.py` emits `REDACTED_SECRET`. Read the authority's constant instead of hard-coding the marker | the secret IS redacted; the assertion is stale |
| 14 | **T5** | `tests/test_module_reachability.py` | 38 modules have no production importer and are unrecorded — **36 of the 38 are the terminals' own module-local `test_*.py` pin files**; plus one stale exemption (`extensions.skill_policy`) | a pin nobody runs is a comment with a filename |
| 15 | **T5 + T1** | `provenance/binaries.json` | commit the eight `rg`/`fd` digests under `committed_digests` | §4.3 |

---

## 7. What Phase 1 established that Phase 2 inherits

* **G1 is RED and published.** `python -m evals.gates.P1` exits 2 with every row
  in the four-status vocabulary.
* **Rung #8 passes and is demonstrated.** 56 turns, 55 distinct real edits, the
  cap declared *and announced*, and the turn-2 constraint still governing at
  turn 45. T1 fixed the constraint decay during P1/W2 — the mechanism is
  `harness/agent_loop_step.py::_constraint_block`, which renders
  `[CONSTRAINTS - still in force]` on **every tool result**. A constraint stated
  once in a rolling message list decays; re-stating it last is what keeps it.
* **All five rung checks have been seen to fire.** `python -m
  evals.rung_break_demos` is a gate row, not a transcript: a rung whose check
  has never failed is decoration, and Phase 2 should keep it that way.
* **The six CI lanes exist, the registry gates every one of them, and a promoted
  pin fails the build with `PROMOTE THIS`.**
* **The daily-driver readiness gate is still `NOT_READY`, and
  `cost_latency_user_intervention_at_least_90pct` is still `UNMEASURED`.** It is
  recorded as `not_implemented` with an owner (T5 / P1.4), not as a row that
  might quietly turn green.